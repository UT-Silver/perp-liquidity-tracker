"""Band depth: resting notional within ±X bps of mid, per side.

Comparable across venues (unlike "everything we happened to fetch"), as long
as the fetched book reaches the band edge. `reach_bps` records how far the
snapshot extends; when reach < band the band value is only a lower bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from perp_liquidity.fetchers.base import OrderBook

DEFAULT_BANDS_BPS: tuple[int, ...] = (10, 50, 100)


@dataclass(frozen=True)
class DepthResult:
    venue: str
    token: str
    side: Literal["bid", "ask"]
    mid_price: float
    reach_bps: float                 # distance of the farthest captured level from mid
    depth_usd: dict[int, float]      # band_bps -> notional within band

    def truncated(self, band_bps: int) -> bool:
        return self.reach_bps < band_bps


def compute_depth(book: OrderBook, bands_bps: tuple[int, ...] = DEFAULT_BANDS_BPS) -> list[DepthResult]:
    mid = book.mid_price
    if not mid:
        return []
    out = []
    for side, levels in (("bid", book.bids), ("ask", book.asks)):
        if not levels:
            continue
        dist = [abs(lv.price - mid) / mid * 10_000 for lv in levels]
        reach = max(dist)
        depth = {
            b: sum(lv.price * lv.qty for lv, d in zip(levels, dist) if d <= b + 1e-9)
            for b in bands_bps
        }
        out.append(DepthResult(book.venue, book.token, side, mid, reach, depth))
    return out


@dataclass(frozen=True)
class OrderStructure:
    venue: str
    token: str
    side: Literal["bid", "ask"]
    top_orders: int                      # resting orders at the best price
    orders: dict[int, int]               # band_bps -> resting orders within band
    makers: dict[int, int] | None        # band_bps -> distinct maker accounts (if known)
    reach_bps: float


def compute_orders(book: OrderBook, bands_bps: tuple[int, ...] = DEFAULT_BANDS_BPS) -> list[OrderStructure]:
    """Order counts per side. Only for venues whose levels carry counts (n) or owners."""
    mid = book.mid_price
    if not mid:
        return []
    out = []
    for side, levels in (("bid", book.bids), ("ask", book.asks)):
        if not levels or all(lv.n is None for lv in levels):
            continue
        best = levels[0].price
        dist = [abs(lv.price - mid) / mid * 10_000 for lv in levels]
        orders = {b: sum((lv.n or 0) for lv, d in zip(levels, dist) if d <= b + 1e-9) for b in bands_bps}
        top = sum((lv.n or 0) for lv in levels if lv.price == best)
        makers = None
        if any(lv.owner for lv in levels):
            makers = {b: len({lv.owner for lv, d in zip(levels, dist) if d <= b + 1e-9 and lv.owner}) for b in bands_bps}
        out.append(OrderStructure(book.venue, book.token, side, top, orders, makers, max(dist)))
    return out
