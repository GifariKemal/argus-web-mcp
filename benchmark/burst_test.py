"""LIVE burst resilience test - does Argus's backoff + multi-engine redundancy
survive BURSTY (un-paced) use that earlier throttled single-engine clients?

Earlier finding: ~5-10 rapid queries from one IP throttled brave/google/ddg to
empties. `argus.search.search` now auto-retries transient `search_backend_down`
with exponential backoff AND fans `general` queries across many engines
(ddg,bing,brave,mojeek,startpage,qwant) so no single engine is a point of failure.

This script fires 15 distinct queries back-to-back (no pacing) twice:

  ARM A (Argus-resilient) - via `argus.search.search` (backoff + redundancy on).
  ARM B (naive-raw)       - a plain httpx GET to SearXNG `/search?...&format=json`
                            with ONLY `categories=general`: no `engines=` fan-out,
                            no retry, no backoff. Isolates the improvement's effect.

Requires SearXNG live at SEARX_URL (default http://127.0.0.1:8888).
Run:  ./.venv/Scripts/python.exe benchmark/burst_test.py
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

import httpx

from argus.search import SearchError, search

SEARX_URL = "http://127.0.0.1:8888"
TIMEOUT = 15.0

# 15 distinct, varied real queries - a few drawn from each scenario category
# (dev / firmware / trading / mql5 / web / ai_ml / news / docs / science / business).
BURST_QUERIES: list[str] = [
    "python 3.13 free-threaded GIL removal status",            # dev
    "difference between git merge and git rebase",             # dev
    "ESP32 deep sleep current consumption microamps",          # firmware
    "Modbus RTU CRC16 calculation algorithm",                  # firmware
    "XAUUSD gold price drivers real yields DXY correlation",   # trading
    "Kelly criterion position sizing formula",                 # trading
    "MQL5 OnTick event handler structure example",             # mql5
    "React 19 use hook server components",                     # web
    "CSS :has() selector parent styling examples",             # web
    "transformer attention mechanism scaled dot product",      # ai_ml
    "RAG retrieval augmented generation chunking strategy",    # ai_ml
    "Federal Reserve interest rate decision June 2026",        # news
    "pydantic v2 model_validate vs parse_obj",                 # docs
    "CRISPR Cas9 mechanism of action explained",               # science
    "how to calculate customer acquisition cost CAC",          # business
]


def _classify(exc: SearchError) -> str:
    return "throttled" if exc.code == "search_backend_down" else "no_results"


async def run_argus_arm(client: httpx.AsyncClient) -> list[dict]:
    """ARM A - fire all queries back-to-back through Argus's resilient search."""
    rows: list[dict] = []
    for q in BURST_QUERIES:
        t0 = time.perf_counter()
        try:
            res = await search(q, count=10, base_url=SEARX_URL, client=client)
            rows.append(
                {
                    "query": q,
                    "status": "ok",
                    "result_count": res["count"],
                    "engines": res["engines_used"],
                    "latency": time.perf_counter() - t0,
                }
            )
        except SearchError as exc:
            rows.append(
                {
                    "query": q,
                    "status": _classify(exc),
                    "result_count": 0,
                    "engines": [],
                    "latency": time.perf_counter() - t0,
                }
            )
    return rows


async def run_naive_arm(client: httpx.AsyncClient) -> list[dict]:
    """ARM B - same 15 queries via a raw SearXNG GET with NO backoff/redundancy.

    Single page, `categories=general` only (no `engines=` fan-out), no retry.
    A transport/HTTP/JSON failure is counted as `throttled`; an empty `results`
    list with reported `unresponsive_engines` is also `throttled`; an empty list
    with none unresponsive is `no_results`.
    """
    rows: list[dict] = []
    for q in BURST_QUERIES:
        t0 = time.perf_counter()
        params = {"q": q, "format": "json", "categories": "general"}
        try:
            resp = await client.get(f"{SEARX_URL}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            rows.append(
                {"query": q, "status": "throttled", "result_count": 0,
                 "engines": [], "latency": time.perf_counter() - t0}
            )
            continue
        results = data.get("results", []) or []
        unresponsive = data.get("unresponsive_engines", []) or []
        engines = sorted({r.get("engine", "") for r in results if r.get("engine")})
        if results:
            status = "ok"
        elif unresponsive:
            status = "throttled"
        else:
            status = "no_results"
        rows.append(
            {"query": q, "status": status, "result_count": len(results),
             "engines": engines, "latency": time.perf_counter() - t0}
        )
    return rows


def summarize(name: str, rows: list[dict]) -> dict:
    n = len(rows)
    ok = [r for r in rows if r["status"] == "ok"]
    throttled = [r for r in rows if r["status"] == "throttled"]
    no_results = [r for r in rows if r["status"] == "no_results"]
    counts = [r["result_count"] for r in ok]
    mean_results = sum(counts) / len(counts) if counts else 0.0
    mean_latency = sum(r["latency"] for r in rows) / n if n else 0.0
    engine_dist: Counter[str] = Counter()
    for r in rows:
        engine_dist.update(r["engines"])

    print(f"\n=== {name} ===")
    print(f"  queries fired:   {n} (back-to-back, no pacing)")
    print(f"  SUCCEEDED (ok):  {len(ok)}/{n}  ({len(ok) / n:.0%})")
    print(f"  throttled:       {len(throttled)}/{n}  ({len(throttled) / n:.0%})")
    print(f"  no_results:      {len(no_results)}/{n}  ({len(no_results) / n:.0%})")
    print(f"  mean results:    {mean_results:.1f} (over successful queries)")
    print(f"  mean latency:    {mean_latency:.2f}s")
    print(f"  engine dist:     {dict(engine_dist.most_common())}")
    if throttled:
        print("  throttled queries:")
        for r in throttled:
            print(f"    - {r['query']}")
    return {
        "n": n,
        "ok": len(ok),
        "throttled": len(throttled),
        "no_results": len(no_results),
        "success_rate": len(ok) / n if n else 0.0,
        "throttle_rate": len(throttled) / n if n else 0.0,
    }


async def main() -> None:
    print(f"BURST resilience test - {len(BURST_QUERIES)} queries, SearXNG @ {SEARX_URL}")
    print("Firing back-to-back with NO pacing (this is the burst).")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # ARM A first: Argus must survive the burst it itself generates.
        argus_rows = await run_argus_arm(client)
        # ARM B immediately after, same hot IP, raw single-engine no-retry.
        naive_rows = await run_naive_arm(client)

    a = summarize("ARM A - Argus-resilient (backoff + multi-engine)", argus_rows)
    b = summarize("ARM B - naive-raw (categories=general, no retry)", naive_rows)

    print("\n=== VERDICT ===")
    print(f"  Argus success {a['success_rate']:.0%} vs naive {b['success_rate']:.0%}  "
          f"(delta {a['success_rate'] - b['success_rate']:+.0%})")
    print(f"  Argus throttle {a['throttle_rate']:.0%} vs naive {b['throttle_rate']:.0%}  "
          f"(delta {a['throttle_rate'] - b['throttle_rate']:+.0%})")
    if a["throttled"] == 0 and b["throttled"] == 0:
        print("  NOTE: neither arm throttled - engines are healthy right now; the "
              "burst did NOT trigger rate-limiting, so this run does not exercise "
              "recovery. Numbers above stand on their own.")
    elif a["success_rate"] > b["success_rate"] or a["throttle_rate"] < b["throttle_rate"]:
        recovered = b["throttled"] - a["throttled"]
        print(f"  Backoff + redundancy MEASURABLY helped: Argus recovered ~{recovered} "
              "queries the naive client lost to throttling.")
    else:
        print("  No measurable advantage on this run.")


if __name__ == "__main__":
    asyncio.run(main())
