#!/usr/bin/env python3
"""TCP relay :6000 -> 127.0.0.1:6001.

Port 6000 is on the WHATWG fetch "bad port" list (X11), so Node clients such as
KimiCode refuse it with `Error: bad port` while curl/python work fine. The
compactor therefore listens on 6001 (Node-reachable) and this relay keeps :6000
answering as well, so BOTH ports reach the SAME compactor instance and share one
sticky-compaction state. Two uvicorn instances would not: their STATE dicts are
per-process, which would silently break summary stickiness if traffic split.
"""
import asyncio, sys
DST_HOST, DST_PORT, LISTEN = "127.0.0.1", 6001, 6000

async def pipe(r, w):
    try:
        while True:
            b = await r.read(65536)
            if not b: break
            w.write(b); await w.drain()
    except Exception: pass
    finally:
        try: w.close()
        except Exception: pass

async def handle(cr, cw):
    try:
        sr, sw = await asyncio.open_connection(DST_HOST, DST_PORT)
    except Exception:
        cw.close(); return
    await asyncio.gather(pipe(cr, sw), pipe(sr, cw))

async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", LISTEN)
    print(f"relay {LISTEN} -> {DST_HOST}:{DST_PORT}", flush=True)
    async with srv: await srv.serve_forever()

asyncio.run(main())
