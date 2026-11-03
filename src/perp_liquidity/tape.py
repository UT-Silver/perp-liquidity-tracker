"""Trade-tape collector: realized execution cost from public fills.

Streams public trades plus top-of-book from Hyperliquid and Lighter, groups
fills into taker "parent" orders (same tx hash), and for each parent records:

  cost_bps   side * (VWAP - pre-trade mid) / mid      what the taker actually paid
                                                      (half spread + walking the book)
  imp10_bps  side * (mid 10 s later - pre mid) / mid  price impact that persisted
  imp60_bps  side * (mid 60 s later - pre mid) / mid
  mo60_bps   side * (mid 60 s later - VWAP) / VWAP    taker markout: > 0 means the
                                                      price kept moving the taker's way

side = +1 for taker buys, -1 for taker sells. Hourly aggregates per
(venue, token, size bucket) go to data/YYYY-MM/tape/{TOKEN}.csv; parents of
$100k+ are also kept individually in data/YYYY-MM/tape_large/{TOKEN}.csv.

Run (long-lived, e.g. systemd on a small VPS):
    python -m perp_liquidity.tape --data-dir data --push
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import json
import logging
import os
import subprocess
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets

log = logging.getLogger("tape")

HL_WS = "wss://api.hyperliquid.xyz/ws"
LT_WS = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
LT_REST = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails"

GROUP_WINDOW_S = 2.0        # fills of one taker tx can arrive in several messages
MARKOUT_S = (10, 60)
MID_HISTORY_S = 180
BUCKETS = [(0, 10_000, "<10k"), (10_000, 100_000, "10k-100k"),
           (100_000, 1_000_000, "100k-1M"), (1_000_000, float("inf"), ">=1M")]
LARGE_USD = 100_000

AGG_COLS = ["hour", "venue", "bucket", "n", "notional_usd",
            "cost_med_bps", "cost_p90_bps", "imp10_med_bps", "imp60_med_bps", "mo60_med_bps"]
LARGE_COLS = ["time", "venue", "side", "notional_usd", "n_fills", "pre_mid", "vwap",
              "cost_bps", "imp10_bps", "imp60_bps", "mo60_bps"]


def bucket_of(usd: float) -> str:
    return next(name for lo, hi, name in BUCKETS if lo <= usd < hi)


def quantile(xs: list[float], q: float) -> float | None:
    x = sorted(v for v in xs if v is not None)
    if not x:
        return None
    i = (len(x) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(x) - 1)
    return x[lo] + (x[hi] - x[lo]) * (i - lo)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

class MidHistory:
    """Time-ordered mids per (venue, token); lookups strictly before / at-or-after t."""

    def __init__(self):
        self.t: dict[tuple, deque] = defaultdict(deque)
        self.m: dict[tuple, deque] = defaultdict(deque)

    def add(self, key: tuple, t_ms: int, mid: float) -> None:
        ts, ms = self.t[key], self.m[key]
        if ts and t_ms < ts[-1]:
            return                      # out-of-order update: ignore
        ts.append(t_ms); ms.append(mid)
        cutoff = t_ms - MID_HISTORY_S * 1000
        while ts and ts[0] < cutoff:
            ts.popleft(); ms.popleft()

    def before(self, key: tuple, t_ms: int) -> float | None:
        ts = self.t[key]
        i = bisect.bisect_left(ts, t_ms) - 1
        return self.m[key][i] if i >= 0 else None

    def at_or_after(self, key: tuple, t_ms: int) -> float | None:
        ts = self.t[key]
        i = bisect.bisect_left(ts, t_ms)
        return self.m[key][i] if i < len(ts) else None

    def latest_t(self, key: tuple) -> int:
        ts = self.t[key]
        return ts[-1] if ts else 0


@dataclass
class Parent:
    venue: str
    token: str
    side: int
    t_ms: int                 # first fill time
    seen_at: float            # wall clock when first fill arrived
    qty: float = 0.0
    usd: float = 0.0
    n: int = 0
    pre_mid: float | None = None

    @property
    def vwap(self) -> float:
        return self.usd / self.qty if self.qty else 0.0


@dataclass
class Collector:
    data_dir: Path
    push: bool = False
    flush_seconds: int = 3600
    mids: MidHistory = field(default_factory=MidHistory)
    open_parents: dict = field(default_factory=dict)        # (venue, token, txid) -> Parent
    awaiting_markout: list = field(default_factory=list)    # closed Parents
    done: dict = field(default_factory=lambda: defaultdict(list))   # (hour, venue, token, bucket) -> [rec]
    large: dict = field(default_factory=lambda: defaultdict(list))  # (month, token) -> [row]
    stats: dict = field(default_factory=lambda: defaultdict(int))

    # ---- ingest ------------------------------------------------------------
    def on_mid(self, venue: str, token: str, t_ms: int, mid: float) -> None:
        self.mids.add((venue, token), t_ms, mid)

    def on_fill(self, venue: str, token: str, txid: str, side: int, t_ms: int, px: float, qty: float) -> None:
        key = (venue, token, txid)
        p = self.open_parents.get(key)
        if p is None:
            p = Parent(venue, token, side, t_ms, time.time())
            p.pre_mid = self.mids.before((venue, token), t_ms)
            self.open_parents[key] = p
        p.qty += qty; p.usd += px * qty; p.n += 1
        self.stats[f"fills_{venue}"] += 1

    # ---- lifecycle -----------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        for key in [k for k, p in self.open_parents.items() if now - p.seen_at >= GROUP_WINDOW_S]:
            self.awaiting_markout.append(self.open_parents.pop(key))
        still = []
        for p in self.awaiting_markout:
            k = (p.venue, p.token)
            if self.mids.latest_t(k) < p.t_ms + MARKOUT_S[-1] * 1000:
                if now - p.seen_at < MARKOUT_S[-1] + 120:
                    still.append(p); continue       # wait for later mids (or give up after 3 min)
            self._finalize(p)
        self.awaiting_markout = still

    def _finalize(self, p: Parent) -> None:
        if not p.pre_mid or p.qty <= 0:
            self.stats["dropped_no_mid"] += 1
            return
        k = (p.venue, p.token)
        def sbps(a, b):
            return None if a is None or not b else p.side * (a - b) / b * 1e4
        m10 = self.mids.at_or_after(k, p.t_ms + 10_000)
        m60 = self.mids.at_or_after(k, p.t_ms + 60_000)
        rec = {"usd": p.usd, "cost": sbps(p.vwap, p.pre_mid), "imp10": sbps(m10, p.pre_mid),
               "imp60": sbps(m60, p.pre_mid), "mo60": sbps(m60, p.vwap)}
        dt = datetime.fromtimestamp(p.t_ms / 1000, tz=timezone.utc)
        hour = dt.replace(minute=0, second=0, microsecond=0).isoformat()
        self.done[(hour, p.venue, p.token, bucket_of(p.usd))].append(rec)
        self.stats[f"parents_{p.venue}"] += 1
        if p.usd >= LARGE_USD:
            self.large[(dt.strftime("%Y-%m"), p.token)].append({
                "time": dt.isoformat(timespec="milliseconds"), "venue": p.venue, "side": "buy" if p.side > 0 else "sell",
                "notional_usd": round(p.usd, 2), "n_fills": p.n, "pre_mid": p.pre_mid, "vwap": round(p.vwap, 8),
                **{f"{x}_bps": None if rec[x] is None else round(rec[x], 3) for x in ("cost", "imp10", "imp60", "mo60")}})

    # ---- output ------------------------------------------------------------
    def flush(self, final: bool = False) -> int:
        """Write aggregates for completed hours (all hours when final)."""
        cur_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        rows: dict[tuple, list] = defaultdict(list)
        for key in sorted(k for k in self.done if final or k[0] < cur_hour):
            hour, venue, token, bucket = key
            recs = self.done.pop(key)
            r = lambda f, q: (None if (v := quantile([x[f] for x in recs], q)) is None else round(v, 3))
            rows[(hour[:7], token)].append({
                "hour": hour, "venue": venue, "bucket": bucket, "n": len(recs),
                "notional_usd": round(sum(x["usd"] for x in recs), 0),
                "cost_med_bps": r("cost", .5), "cost_p90_bps": r("cost", .9),
                "imp10_med_bps": r("imp10", .5), "imp60_med_bps": r("imp60", .5), "mo60_med_bps": r("mo60", .5)})
        written = 0
        for (month, token), rs in rows.items():
            written += _append(self.data_dir / month / "tape" / f"{token}.csv", AGG_COLS, rs)
        for (month, token), rs in list(self.large.items()):
            written += _append(self.data_dir / month / "tape_large" / f"{token}.csv", LARGE_COLS, rs)
            del self.large[(month, token)]
        if written:
            # manifest.json is owned by the hourly snapshot job (it scans data/*/tape),
            # so the collector only ever touches its own files: no merge conflicts.
            if self.push:
                git_push(self.data_dir, f"tape {cur_hour[:13]}Z")
        return written


def _append(path: Path, cols: list[str], rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


def git_push(data_dir: Path, msg: str) -> None:
    repo = data_dir.resolve().parent
    def g(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=120)
    g("add", "data/*/tape", "data/*/tape_large")
    if g("diff", "--cached", "--quiet").returncode == 0:
        return
    g("commit", "-q", "-m", msg)
    for _ in range(3):
        if g("pull", "-q", "--rebase").returncode == 0 and g("push", "-q").returncode == 0:
            return
        time.sleep(5)
    log.error("git push failed")


# --------------------------------------------------------------------------
# venue streams
# --------------------------------------------------------------------------

async def hl_stream(c: Collector, tokens: list[str]) -> None:
    while True:
        try:
            async with websockets.connect(HL_WS, ping_interval=20, max_size=2**23) as ws:
                for tok in tokens:
                    for sub in ({"type": "trades", "coin": tok}, {"type": "bbo", "coin": tok}):
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                log.info("hyperliquid connected (%d tokens)", len(tokens))
                async for raw in ws:
                    msg = json.loads(raw)
                    ch, d = msg.get("channel"), msg.get("data")
                    if ch == "bbo":
                        bid, ask = d["bbo"]
                        if bid and ask:
                            c.on_mid("hyperliquid", d["coin"], int(d["time"]), (float(bid["px"]) + float(ask["px"])) / 2)
                    elif ch == "trades":
                        for tr in d:
                            # side = taker side: "B" buy, "A" sell; fills of one taker order share the tx hash
                            side = 1 if tr["side"] == "B" else -1
                            txid = tr.get("hash") or f'{tr["time"]}-{tr["side"]}'
                            c.on_fill("hyperliquid", tr["coin"], txid, side, int(tr["time"]), float(tr["px"]), float(tr["sz"]))
        except Exception as e:  # noqa: BLE001 - reconnect on anything
            log.warning("hyperliquid stream error: %s; reconnecting", e)
            await asyncio.sleep(3)


async def lighter_markets(tokens: list[str]) -> dict[int, str]:
    async with httpx.AsyncClient() as h:
        d = (await h.get(LT_REST, params={"filter": "perp"}, timeout=15)).json()
    want = set(tokens)
    return {m["market_id"]: m["symbol"] for m in d["order_book_details"]
            if m.get("symbol") in want and m.get("status") == "active"}


async def lighter_stream(c: Collector, tokens: list[str]) -> None:
    while True:
        try:
            markets = await lighter_markets(tokens)
            books: dict[int, dict] = {mid: {"bids": {}, "asks": {}} for mid in markets}
            async with websockets.connect(LT_WS, ping_interval=20, max_size=2**23) as ws:
                for mid in markets:
                    for ch in (f"order_book/{mid}", f"trade/{mid}"):
                        await ws.send(json.dumps({"type": "subscribe", "channel": ch}))
                log.info("lighter connected (%d markets)", len(markets))
                async for raw in ws:
                    msg = json.loads(raw)
                    typ, ch = msg.get("type", ""), msg.get("channel", "")
                    if typ.endswith("/order_book"):
                        mid_id = int(ch.split(":")[1]); ob = msg["order_book"]; bk = books[mid_id]
                        if typ.startswith("subscribed"):
                            bk["bids"].clear(); bk["asks"].clear()
                        for side in ("bids", "asks"):
                            for lv in ob.get(side, []):
                                px, sz = float(lv["price"]), float(lv["size"])
                                if sz == 0:
                                    bk[side].pop(px, None)
                                else:
                                    bk[side][px] = sz
                        if bk["bids"] and bk["asks"]:
                            bb, ba = max(bk["bids"]), min(bk["asks"])
                            if bb < ba:
                                t_ms = int(msg.get("last_updated_at", time.time() * 1e6)) // 1000
                                c.on_mid("lighter", markets[mid_id], t_ms, (bb + ba) / 2)
                    elif typ == "update/trade":
                        for tr in msg.get("trades") or []:
                            if tr.get("type") != "trade":
                                continue
                            # is_maker_ask: maker was the ask -> taker bought (taker = bid order)
                            side = 1 if tr.get("is_maker_ask") else -1
                            # tx_hash differs per fill on Lighter; group by the taker's order id instead
                            taker_oid = tr.get("bid_id_str" if side > 0 else "ask_id_str") or tr.get("tx_hash")
                            tok = markets.get(int(tr["market_id"]))
                            if tok:
                                c.on_fill("lighter", tok, f"{side}:{taker_oid}", side, int(tr["timestamp"]),
                                          float(tr["price"]), float(tr["size"]))
        except Exception as e:  # noqa: BLE001
            log.warning("lighter stream error: %s; reconnecting", e)
            await asyncio.sleep(3)


async def run(c: Collector, tokens: list[str], duration: float | None) -> None:
    tasks = [asyncio.create_task(hl_stream(c, tokens)), asyncio.create_task(lighter_stream(c, tokens))]
    start = last_flush = time.time()
    try:
        while duration is None or time.time() - start < duration:
            await asyncio.sleep(1)
            c.tick()
            if time.time() - last_flush >= c.flush_seconds:
                n = c.flush(); last_flush = time.time()
                log.info("flushed %d rows; %s", n, dict(c.stats))
    finally:
        for t in tasks:
            t.cancel()
        c.tick(time.time() + 10_000)       # close everything still open
        n = c.flush(final=True)
        log.info("final flush %d rows; %s", n, dict(c.stats))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--tokens", default=os.getenv("TAPE_TOKENS", "BTC,ETH,SOL,HYPE,XRP,DOGE,SUI,ENA,ZEC,PUMP"))
    ap.add_argument("--push", action="store_true", help="git commit + push after each flush")
    ap.add_argument("--flush-seconds", type=int, default=600, help="how often to write completed hours")
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds (testing)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    c = Collector(Path(a.data_dir), push=a.push, flush_seconds=a.flush_seconds)
    asyncio.run(run(c, [t.strip().upper() for t in a.tokens.split(",") if t.strip()], a.duration))


if __name__ == "__main__":
    main()
