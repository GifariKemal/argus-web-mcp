"""Local load / leak sanity test for Argus (P3 staging gate, run before VPS deploy).

Two phases:
  1. STATIC FLOOD - N distinct URLs through the real `read` tool (fetch->extract->cache),
     served by an in-process MockTransport (offline, no live net). Stresses the asyncio
     concurrency, extraction, and cache write paths; watches RSS for leaks.
  2. BROWSER SATURATION - M concurrent real Chromium renders of a single safe page,
     bounded by the BrowserPool semaphore. Asserts active_contexts never exceeds the
     pool concurrency and the browser survives, watches RSS across rounds.

Usage:  ./.venv/Scripts/python.exe benchmark/loadtest.py [--static N] [--browser M] [--rounds R]
This is NOT a unit test (it needs a real browser + is slow); it is an operational check.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import time

import httpx
import psutil

from argus import server
from argus.cache import Cache
from argus.fetch.render import BrowserPool

_ARTICLE = "<html><body><article>" + ("Load test sentence here. " * 60) + "</article></body></html>"


def _public_dns():
    def _gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    socket.getaddrinfo = _gai  # process-wide for this script only


def _mock_client() -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ARTICLE)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _rss_mb() -> float:
    return psutil.Process().memory_info().rss / (1024 * 1024)


async def static_flood(n: int, concurrency: int) -> dict:
    sem = asyncio.Semaphore(concurrency)
    errors = 0

    async def one(i: int):
        nonlocal errors
        async with sem:
            r = await server.read(f"http://load.test/article/{i}")
            if "code" in r:
                errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(n)))
    dt = time.perf_counter() - t0
    return {"n": n, "errors": errors, "seconds": round(dt, 2), "rps": round(n / dt, 1)}


async def browser_saturation(pool: BrowserPool, m: int, url: str) -> dict:
    peak = 0
    errors = 0

    async def one():
        nonlocal peak, errors
        try:
            await pool.render(url, timeout=45)
            peak = max(peak, pool.active_contexts)
        except Exception:  # noqa: BLE001 - count, don't abort the flood
            errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(m)))
    dt = time.perf_counter() - t0
    return {"m": m, "errors": errors, "peak_active": peak, "concurrency": pool._concurrency,
            "seconds": round(dt, 2)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", type=int, default=1000)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--browser", type=int, default=24)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--url", default="https://example.com/")
    args = ap.parse_args()

    _public_dns()
    print(f"start RSS: {_rss_mb():.1f} MB")

    # Phase 1: static flood (offline) across rounds - leak check.
    server._S = server.State(
        client=_mock_client(),
        cache=Cache(db_path="./.argus/loadtest.db", blob_dir="./.argus/loadtest_blobs"),
        browser=None,
    )
    for r in range(args.rounds):
        res = await static_flood(args.static, args.concurrency)
        print(f"[static r{r+1}] {res} | RSS {_rss_mb():.1f} MB")
    await server._S.client.aclose()
    server._S.cache.close()

    # Phase 2: browser saturation - semaphore bound + Chromium stability + leak.
    pool = BrowserPool(concurrency=4)
    await pool.start()
    try:
        for r in range(args.rounds):
            res = await browser_saturation(pool, args.browser, args.url)
            ok = res["peak_active"] <= res["concurrency"]
            print(f"[browser r{r+1}] {res} | semaphore-bound={'OK' if ok else 'VIOLATED'} "
                  f"| RSS {_rss_mb():.1f} MB")
            assert ok, "active_contexts exceeded pool concurrency - semaphore bug"
    finally:
        await pool.stop()

    print(f"end RSS: {_rss_mb():.1f} MB")


if __name__ == "__main__":
    asyncio.run(main())
