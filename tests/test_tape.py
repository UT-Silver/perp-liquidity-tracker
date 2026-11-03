"""Offline tests for the trade-tape collector's parent grouping and cost math."""
import csv

import pytest

from perp_liquidity.tape import Collector, MidHistory, bucket_of


def test_mid_history_before_and_after():
    m = MidHistory(); k = ("v", "BTC")
    for t, mid in [(1000, 100.0), (2000, 101.0), (3000, 102.0)]:
        m.add(k, t, mid)
    assert m.before(k, 2000) == 100.0          # strictly before
    assert m.before(k, 2001) == 101.0
    assert m.at_or_after(k, 2000) == 101.0
    assert m.before(k, 500) is None and m.at_or_after(k, 9999) is None


def test_parent_cost_impact_markout(tmp_path):
    c = Collector(tmp_path)
    k = ("hyperliquid", "BTC")
    c.on_mid(*k, 1_000, 100.00)                 # pre-trade mid
    # taker buy of 2 units walking two levels, two messages, same tx
    c.on_fill(*k, "0xabc", +1, 1_500, 100.01, 1.0)
    c.on_fill(*k, "0xabc", +1, 1_500, 100.03, 1.0)
    c.on_mid(*k, 11_600, 100.05)                # +10 s
    c.on_mid(*k, 61_600, 100.10)                # +60 s
    c.tick(now=1e12)                            # close + finalize
    (key, recs), = c.done.items()
    r = recs[0]
    assert key[1:] == ("hyperliquid", "BTC", "<10k")
    assert r["cost"] == pytest.approx(2.0, abs=1e-6)     # vwap 100.02 vs 100.00
    assert r["imp10"] == pytest.approx(5.0, abs=1e-6)
    assert r["imp60"] == pytest.approx(10.0, abs=1e-6)
    assert r["mo60"] == pytest.approx((100.10 - 100.02) / 100.02 * 1e4, abs=1e-6)


def test_sell_side_sign_and_large_rows(tmp_path):
    c = Collector(tmp_path)
    k = ("lighter", "ETH")
    c.on_mid(*k, 1_000, 2000.0)
    c.on_fill(*k, "tx1", -1, 2_000, 1999.0, 100.0)      # $199,900 taker sell, 5 bps below mid
    c.on_mid(*k, 70_000, 1998.0)
    c.tick(now=1e12)
    assert c.flush(final=True) >= 2
    month = next(p for p in tmp_path.iterdir())
    agg = list(csv.DictReader((month / "tape" / "ETH.csv").open()))
    assert agg[0]["bucket"] == "100k-1M" and float(agg[0]["cost_med_bps"]) == pytest.approx(5.0)
    large = list(csv.DictReader((month / "tape_large" / "ETH.csv").open()))
    assert large[0]["side"] == "sell" and float(large[0]["imp60_bps"]) == pytest.approx(10.0)


def test_parent_without_pre_mid_is_dropped(tmp_path):
    c = Collector(tmp_path)
    c.on_fill("hyperliquid", "SOL", "x", 1, 5_000, 150.0, 1.0)   # no mid yet
    c.tick(now=1e12)
    assert not c.done and c.stats["dropped_no_mid"] == 1


def test_buckets():
    assert [bucket_of(x) for x in (5e3, 5e4, 5e5, 5e6)] == ["<10k", "10k-100k", "100k-1M", ">=1M"]
