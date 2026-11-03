"""Scheduled snapshot runner: append one row per (venue, token, side, clip) to
monthly CSVs under data/, plus funding / OI, and rewrite data/latest.md.

Usage (what the GitHub Action runs):
    python -m perp_liquidity.tracker

Env overrides:
    TRACK_TOKENS  comma list, default BTC,ETH,SOL,HYPE
    TRACK_CLIPS   comma list of USD sizes, default 10000,100000,500000,1000000
    DATA_DIR      default ./data
    FEES_PATH     default ./config/fees.json
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from perp_liquidity.analyzers.depth import DEFAULT_BANDS_BPS, compute_depth
from perp_liquidity.analyzers.volatility import fetch_vols
from perp_liquidity.cli import run_analysis

# Paths are relative to the working directory (the repo root in CI), not the
# installed package, so a plain `pip install .` still finds config/ and data/.
DEFAULT_FEES_PATH = "config/fees.json"

SLIPPAGE_COLS = [
    "run_ts", "venue", "token", "side", "clip_usd", "mid_price", "spread_bps",
    "slippage_bps", "taker_bps", "total_cost_bps", "partial", "filled_usd",
    "book_depth_usd", "book_fetched_at",
]
FUNDING_COLS = ["run_ts", "venue", "token", "rate_per_period", "period_hours", "apr_pct"]
OI_COLS = ["run_ts", "venue", "token", "oi_base", "oi_usd", "mark_price"]
ERROR_COLS = ["run_ts", "venue", "token", "kind", "message"]
# Per-token slippage files (data/YYYY-MM/slippage/{TOKEN}.csv) keep each file
# small enough for the dashboard to load only the tokens being viewed.
# Fees are not stored: the dashboard prices history from config/fees.json.
DEPTH_COLS = ["run_ts", "venue", "side", "mid_price", "reach_bps"] + [f"depth_{b}bps_usd" for b in DEFAULT_BANDS_BPS]
VOL_COLS = ["run_ts", "n_returns", "rv_1h_bps", "rv_ann_pct", "ret_1h_bps"]
TOKEN_SLIP_COLS = ["run_ts", "venue", "side", "clip_usd", "mid_price", "spread_bps",
                   "slippage_bps", "partial", "filled_usd", "book_depth_usd"]


def load_fees() -> dict[str, float]:
    path = Path(os.getenv("FEES_PATH", DEFAULT_FEES_PATH))
    data = json.loads(path.read_text())
    return {k: float(v["taker_bps"]) for k, v in data["venues"].items()}


def _append(path: Path, cols: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerows(rows)


def _r(x: float | None, nd: int = 4):
    return None if x is None else round(x, nd)


async def snapshot(tokens: list[str], clips: list[float]):
    reports = await asyncio.gather(*(run_analysis(t, clip_usds=clips) for t in tokens))
    return reports


def trailing_rv(data_dir: Path, token: str = "BTC", n: int = 168) -> list[float]:
    """Last n recorded 1h RVs (bps) for token, across monthly files."""
    vals: list[float] = []
    for f in sorted(data_dir.glob(f"*/vol/{token}.csv")):
        with f.open() as fh:
            vals += [float(r["rv_1h_bps"]) for r in csv.DictReader(fh) if r.get("rv_1h_bps")]
    return vals[-n:]


def effective_gap(min_gap: float, data_dir: Path, now_rv_bps: float | None) -> tuple[float, str]:
    """Sample faster when BTC's last-hour realized vol is > 1.5x its trailing median."""
    fast = float(os.getenv("FAST_GAP_MINUTES", "10"))
    hist = trailing_rv(data_dir)
    if now_rv_bps is None or len(hist) < 12:
        return min_gap, "normal"
    med = sorted(hist)[len(hist) // 2]
    if med > 0 and now_rv_bps > 1.5 * med:
        return min(min_gap, fast), f"high-vol (BTC 1h RV {now_rv_bps:.0f} bps vs median {med:.0f})"
    return min_gap, "normal"


def main() -> int:
    tokens = [t.strip().upper() for t in os.getenv("TRACK_TOKENS", "BTC,ETH,SOL,HYPE,DOGE,XRP,PUMP,SUI,ENA,ZEC").split(",") if t.strip()]
    clips = [float(c) for c in os.getenv("TRACK_CLIPS", "10000,100000,500000,1000000").split(",")]
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    fees = load_fees()

    run_ts = datetime.now(timezone.utc).replace(microsecond=0)

    # GitHub cron is lossy, so the workflow fires every 15 min; only snapshot
    # if the last one is older than MIN_GAP_MINUTES (manual runs set FORCE=1).
    min_gap = float(os.getenv("MIN_GAP_MINUTES", "0"))
    manifest_path = data_dir / "manifest.json"
    vols = None
    if min_gap > 0 and os.getenv("FORCE") != "1" and manifest_path.exists():
        try:
            last = datetime.fromisoformat(json.loads(manifest_path.read_text()).get("updated", ""))
            age_min = (run_ts - last).total_seconds() / 60
            if age_min < min_gap:
                vols = asyncio.run(fetch_vols(tokens))
                btc = vols.get("BTC")
                gap, regime = effective_gap(min_gap, data_dir, btc.rv_1h_bps if btc else None)
                if age_min < gap:
                    print(f"skip: last snapshot {age_min:.0f} min ago (< {gap:.0f}, {regime})")
                    return 0
                print(f"sampling early: {regime}")
        except ValueError:
            pass
    ts = run_ts.isoformat()
    month_dir = data_dir / run_ts.strftime("%Y-%m")

    reports = asyncio.run(snapshot(tokens, clips))
    if vols is None:
        vols = asyncio.run(fetch_vols(tokens))

    slip_rows, fund_rows, oi_rows, err_rows = [], [], [], []
    depth_rows: dict[str, list[dict]] = {}
    for rep in reports:
        books = {ob.venue: ob for ob in rep.orderbooks}
        for ob in rep.orderbooks:
            for d in compute_depth(ob):
                row = {"run_ts": ts, "venue": d.venue, "side": d.side,
                       "mid_price": d.mid_price, "reach_bps": round(d.reach_bps, 2)}
                row.update({f"depth_{b}bps_usd": round(v, 0) for b, v in d.depth_usd.items()})
                depth_rows.setdefault(rep.token, []).append(row)
        for s in rep.slippage:
            taker = fees.get(s.venue)
            ob = books.get(s.venue)
            slip_rows.append({
                "run_ts": ts, "venue": s.venue, "token": s.token, "side": s.side,
                "clip_usd": int(s.clip_usd), "mid_price": s.mid_price,
                "spread_bps": _r(ob.spread_bps) if ob else None,
                "slippage_bps": _r(s.slippage_bps),
                "taker_bps": taker,
                "total_cost_bps": None if taker is None else _r(s.slippage_bps + taker),
                "partial": s.partial, "filled_usd": round(s.filled_usd, 2),
                "book_depth_usd": round(s.book_depth_usd, 2),
                "book_fetched_at": ob.fetched_at.replace(microsecond=0).isoformat() if ob else "",
            })
        for row in rep.funding:
            fr = row.funding_rate
            fund_rows.append({"run_ts": ts, "venue": fr.venue, "token": fr.token,
                              "rate_per_period": fr.rate_per_period, "period_hours": fr.period_hours,
                              "apr_pct": _r(fr.apr_annualized, 3)})
        for row in rep.oi:
            oi = row.open_interest
            oi_rows.append({"run_ts": ts, "venue": oi.venue, "token": oi.token,
                            "oi_base": oi.oi_base, "oi_usd": round(oi.oi_usd, 0),
                            "mark_price": oi.mark_price})
        for venue, msg in rep.failures:
            err_rows.append({"run_ts": ts, "venue": venue, "token": rep.token, "kind": "error", "message": msg[:300]})
        for venue in rep.not_listed:
            err_rows.append({"run_ts": ts, "venue": venue, "token": rep.token, "kind": "not_listed", "message": ""})

    for tok in tokens:
        _append(month_dir / "slippage" / f"{tok}.csv", TOKEN_SLIP_COLS,
                [{k: r[k] for k in TOKEN_SLIP_COLS} for r in slip_rows if r["token"] == tok])
    for tok, v in vols.items():
        _append(month_dir / "vol" / f"{tok}.csv", VOL_COLS, [{
            "run_ts": ts, "n_returns": v.n_returns, "rv_1h_bps": round(v.rv_1h_bps, 2),
            "rv_ann_pct": round(v.rv_ann_pct, 2), "ret_1h_bps": round(v.ret_1h_bps, 2)}])
    for tok, rows in depth_rows.items():
        _append(month_dir / "depth" / f"{tok}.csv", DEPTH_COLS, rows)
    _append(month_dir / "funding.csv", FUNDING_COLS, fund_rows)
    _append(month_dir / "open_interest.csv", OI_COLS, oi_rows)
    _append(month_dir / "errors.csv", ERROR_COLS, err_rows)
    write_latest(data_dir / "latest.md", ts, tokens, clips, slip_rows, err_rows)
    write_manifest(data_dir, ts)

    ok_venues = sorted({r["venue"] for r in slip_rows})
    print(f"[{ts}] rows: slippage={len(slip_rows)} funding={len(fund_rows)} oi={len(oi_rows)} errors={len(err_rows)}")
    print(f"venues with data: {', '.join(ok_venues) or 'NONE'}")
    for e in err_rows:
        if e["kind"] == "error":
            print(f"  ERROR {e['venue']} {e['token']}: {e['message']}")
    # Fail the job only if nothing at all came back, so silent breakage is visible.
    return 0 if slip_rows else 1


def write_manifest(data_dir: Path, updated: str) -> None:
    """{"months": [...], "tokens": {"2026-11": ["BTC", ...]}, "updated": ts}"""
    months, tokens, depth, vol = [], {}, {}, {}
    for p in sorted(data_dir.iterdir()):
        sd = p / "slippage"
        if p.is_dir() and sd.is_dir():
            toks = sorted(f.stem for f in sd.glob("*.csv"))
            if toks:
                months.append(p.name)
                tokens[p.name] = toks
                dd = p / "depth"
                if dd.is_dir():
                    depth[p.name] = sorted(f.stem for f in dd.glob("*.csv"))
                vd = p / "vol"
                if vd.is_dir():
                    vol[p.name] = sorted(f.stem for f in vd.glob("*.csv"))
    (data_dir / "manifest.json").write_text(
        json.dumps({"months": months, "tokens": tokens, "depth": depth, "vol": vol, "updated": updated}) + "\n")


def write_latest(path: Path, ts: str, tokens, clips, slip_rows, err_rows) -> None:
    lines = [f"# Perp slippage snapshot — {ts}", "",
             "Total cost = slippage vs mid + base-tier taker fee, in bps, averaged over buy & sell. "
             "`*` = book snapshot too shallow to fill the clip.", ""]
    for tok in tokens:
        rows = [r for r in slip_rows if r["token"] == tok]
        if not rows:
            continue
        venues = sorted({r["venue"] for r in rows})
        lines.append(f"## {tok}")
        lines.append("")
        hdr = "| venue | spread bps | " + " | ".join(f"${int(c):,}" for c in clips) + " |"
        lines.append(hdr)
        lines.append("|" + "---|" * (2 + len(clips)))
        for v in venues:
            vr = [r for r in rows if r["venue"] == v]
            cells = []
            for c in clips:
                cr = [r for r in vr if r["clip_usd"] == int(c)]
                if not cr or any(r["total_cost_bps"] is None for r in cr):
                    cells.append("n/a")
                    continue
                avg = sum(r["total_cost_bps"] for r in cr) / len(cr)
                star = "*" if any(r["partial"] for r in cr) else ""
                cells.append(f"{avg:.2f}{star}")
            spread = vr[0]["spread_bps"]
            lines.append(f"| {v} | {'' if spread is None else f'{spread:.2f}'} | " + " | ".join(cells) + " |")
        lines.append("")
    errs = [e for e in err_rows if e["kind"] == "error"]
    if errs:
        lines.append("## Errors")
        lines += [f"- {e['venue']} / {e['token']}: {e['message']}" for e in errs]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
