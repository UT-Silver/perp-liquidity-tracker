"""Realized volatility from Hyperliquid 1-minute candles.

Used to tag each snapshot with the market regime it was taken in, so the
dashboard can compare execution cost in calm vs volatile hours, and so the
tracker can sample more often when the market is moving.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

import httpx

HL_INFO = "https://api.hyperliquid.xyz/info"
MINUTES_PER_YEAR = 60 * 24 * 365


@dataclass(frozen=True)
class RealizedVol:
    token: str
    n_returns: int
    rv_1h_bps: float      # sqrt(sum of squared 1m log returns) over the last hour, in bps
    rv_ann_pct: float     # same, annualized, in %
    ret_1h_bps: float     # close-to-close move over the window, in bps


def realized_vol(token: str, closes: list[float]) -> RealizedVol | None:
    if len(closes) < 10:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if not rets:
        return None
    ss = sum(r * r for r in rets)
    rv = math.sqrt(ss)
    ann = math.sqrt(ss / len(rets) * MINUTES_PER_YEAR)
    return RealizedVol(token, len(rets), rv * 1e4, ann * 100, math.log(closes[-1] / closes[0]) * 1e4)


async def fetch_closes(client: httpx.AsyncClient, token: str, minutes: int = 60) -> list[float]:
    end = int(time.time() * 1000)
    body = {"type": "candleSnapshot",
            "req": {"coin": token, "interval": "1m", "startTime": end - (minutes + 1) * 60_000, "endTime": end}}
    r = await client.post(HL_INFO, json=body, timeout=10)
    r.raise_for_status()
    candles = r.json() or []
    candles.sort(key=lambda c: c["t"])
    return [float(c["c"]) for c in candles][-(minutes + 1):]


async def fetch_vols(tokens: list[str], minutes: int = 60) -> dict[str, RealizedVol]:
    out: dict[str, RealizedVol] = {}
    async with httpx.AsyncClient() as client:
        async def one(tok: str):
            try:
                rv = realized_vol(tok, await fetch_closes(client, tok, minutes))
                if rv:
                    out[tok] = rv
            except (httpx.HTTPError, KeyError, ValueError):
                pass   # vol is a tag; a missing value must never block the snapshot
        await asyncio.gather(*(one(t) for t in tokens))
    return out
