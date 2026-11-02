"""Tests for the HL book stitching and the scheduled tracker (no network)."""

from __future__ import annotations

import csv
import json

import httpx
import pytest

from perp_liquidity.analyzers.slippage import compute_slippage
from perp_liquidity.cli import run_analysis
from perp_liquidity.fetchers.base import OrderBook, OrderBookLevel
from perp_liquidity.fetchers.hyperliquid import HyperliquidClient
from perp_liquidity import tracker


def _lv(px, sz):
    return {"px": str(px), "sz": str(sz), "n": 1}


def _hl_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["type"] == "metaAndAssetCtxs":
        return httpx.Response(200, json=[
            {"universe": [{"name": "BTC"}]},
            [{"funding": "0.00001", "openInterest": "10", "markPx": "100000"}],
        ])
    nsig = body.get("nSigFigs")
    if nsig is None:  # fine book: 100000 +/- 1..3, 1 BTC each
        bids = [_lv(99999 - i, 1) for i in range(3)]
        asks = [_lv(100001 + i, 1) for i in range(3)]
    elif nsig == 4:  # $10 buckets, overlapping the fine range
        bids = [_lv(99990 - 10 * i, 2) for i in range(5)]
        asks = [_lv(100010 + 10 * i, 2) for i in range(5)]
    else:  # nSigFigs=3: $100 buckets
        bids = [_lv(99900 - 100 * i, 5) for i in range(5)]
        asks = [_lv(100100 + 100 * i, 5) for i in range(5)]
    return httpx.Response(200, json={"coin": "BTC", "levels": [bids, asks]})


async def test_hl_stitching_extends_book_without_overlap():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_hl_handler))
    async with HyperliquidClient(client) as hl:
        ob = await hl.get_orderbook("BTC")
    await client.aclose()

    ask_px = [a.price for a in ob.asks]
    bid_px = [b.price for b in ob.bids]
    assert ask_px == sorted(ask_px) and len(set(ask_px)) == len(ask_px)
    assert bid_px == sorted(bid_px, reverse=True) and len(set(bid_px)) == len(bid_px)
    # fine levels first, untouched
    assert ask_px[:3] == [100001, 100002, 100003]
    # first coarse bucket beyond the boundary (100010) is skipped as possibly overlapping
    assert 100010 not in ask_px and 100020 in ask_px
    # nSigFigs=3 levels appended beyond the nSigFigs=4 range
    assert ask_px[-1] == 100500
    assert ob.extras["stitched_levels"] > 0


def test_negative_slippage_not_hidden():
    # crossed book: best ask below best bid -> buy fills below mid
    ob = OrderBook(venue="x", token="BTC",
                   bids=[OrderBookLevel(101.0, 10)], asks=[OrderBookLevel(99.0, 10)])
    r = compute_slippage(ob, 100, "buy")
    assert r.slippage_bps < 0
    assert r.book_depth_usd == pytest.approx(990.0)


async def test_report_carries_venue_on_unexpected_error():
    class Boom(HyperliquidClient):
        VENUE = "boom"

        async def get_orderbook(self, token):
            raise RuntimeError("kaput")

    rep = await run_analysis("BTC", fetcher_classes=[Boom], clip_usds=[1000])
    assert rep.failures and rep.failures[0][0] == "boom"


def test_tracker_writes_history(tmp_path, monkeypatch):
    real_init = httpx.AsyncClient.__init__

    def patched(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(_hl_handler)
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    monkeypatch.setattr("perp_liquidity.cli._default_fetchers", lambda: [HyperliquidClient])
    monkeypatch.setenv("TRACK_TOKENS", "BTC")
    monkeypatch.setenv("TRACK_CLIPS", "100000,5000000")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FEES_PATH", str(tracker.Path(__file__).parents[1] / "config" / "fees.json"))

    assert tracker.main() == 0
    assert tracker.main() == 0  # second run appends, header written once

    month = next(p for p in tmp_path.iterdir() if p.is_dir())
    rows = list(csv.DictReader((month / "slippage" / "BTC.csv").open()))
    assert len(rows) == 2 * 2 * 2  # runs x clips x sides
    r = next(r for r in rows if r["clip_usd"] == "100000" and r["side"] == "buy")
    assert "token" not in r and "taker_bps" not in r  # token from filename, fees from config
    assert r["spread_bps"] != "" and float(r["slippage_bps"]) >= 0
    big = next(r for r in rows if r["clip_usd"] == "5000000")
    assert big["partial"] == "True"  # stitched mock book holds ~$3.1M per side
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["months"] == [month.name]
    assert manifest["tokens"][month.name] == ["BTC"]
    latest = (tmp_path / "latest.md").read_text()
    assert "hyperliquid" in latest and "$100,000" in latest


def test_tracker_skips_when_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MIN_GAP_MINUTES", "45")
    monkeypatch.delenv("FORCE", raising=False)
    from datetime import datetime, timezone
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"months": [], "updated": datetime.now(timezone.utc).isoformat()}))
    called = []
    monkeypatch.setattr(tracker, "snapshot", lambda *a: called.append(1))
    assert tracker.main() == 0 and not called
