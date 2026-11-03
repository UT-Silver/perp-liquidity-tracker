"""Dump a few raw WS messages from Hyperliquid and Lighter (format discovery)."""
import asyncio, json, time
import httpx, websockets

async def hl():
    async with websockets.connect("wss://api.hyperliquid.xyz/ws", ping_interval=20) as ws:
        for sub in ({"type": "trades", "coin": "BTC"}, {"type": "bbo", "coin": "BTC"}):
            await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
        seen = {}; end = time.time() + 15
        while time.time() < end:
            try: m = json.loads(await asyncio.wait_for(ws.recv(), 5))
            except asyncio.TimeoutError: continue
            ch = m.get("channel"); seen[ch] = seen.get(ch, 0) + 1
            if seen[ch] <= 2: print("HL", ch, json.dumps(m)[:700])
        print("HL counts", seen)

async def lighter():
    async with httpx.AsyncClient() as c:
        d = (await c.get("https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails", params={"filter": "perp"})).json()
    mid = next(m["market_id"] for m in d["order_book_details"] if m["symbol"] == "BTC")
    print("lighter BTC market_id", mid)
    async with websockets.connect("wss://mainnet.zklighter.elliot.ai/stream", ping_interval=20) as ws:
        for ch in (f"trade/{mid}", f"order_book/{mid}", f"market_stats/{mid}"):
            await ws.send(json.dumps({"type": "subscribe", "channel": ch}))
        seen = {}; end = time.time() + 15
        while time.time() < end:
            try: m = json.loads(await asyncio.wait_for(ws.recv(), 5))
            except asyncio.TimeoutError: continue
            k = (m.get("type"), m.get("channel")); seen[k] = seen.get(k, 0) + 1
            if seen[k] <= 2: print("LT", k, json.dumps(m)[:900])
        print("LT counts", seen)

async def main():
    await asyncio.gather(hl(), lighter(), return_exceptions=False)
asyncio.run(main())
