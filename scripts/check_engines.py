#!/usr/bin/env python3
"""Measure which SearXNG engines actually answer from THIS host.

Free engines block or CAPTCHA whole IP ranges, and the block differs per provider, so
the useful fan-out is a property of the network you run on - not something to guess.
Run this after moving hosts, after changing a proxy, or when search quality drops.

    python scripts/check_engines.py                       # against the default backend
    python scripts/check_engines.py --url http://searxng:8080
    python scripts/check_engines.py --engines bing,brave --queries 5

Exit code is 1 when an engine that was asked for answered nothing, so it doubles as a
post-deploy gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

DEFAULT_ENGINES = "bing,brave,google,google cse,duckduckgo web,yandex"
# Varied, unremarkable queries: one repeated term would measure caching, not the engine.
QUERIES = ["climate model", "rust ownership", "postgres vacuum", "esp32 nvs", "traefik acme"]


def probe(base: str, engine: str, query: str, timeout: float) -> tuple[int, float, str]:
    url = f"{base}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "engines": engine}
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed http(s) base
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
        return 0, time.monotonic() - started, type(exc).__name__
    reasons = [
        r[1] if isinstance(r, (list, tuple)) and len(r) > 1 else str(r)
        for r in (data.get("unresponsive_engines") or [])
        if (r[0] if isinstance(r, (list, tuple)) else r) == engine
    ]
    # Count only what THIS engine returned. SearXNG drops an `engines=` name it does not
    # know and answers from the default set instead, so a total-result count scores a
    # nonexistent engine as healthy on other engines' results - which is exactly how
    # `startpage` and `marginalia` passed for months after upstream removed them.
    mine = sum(1 for r in data.get("results", []) if r.get("engine") == engine)
    if not mine and data.get("results"):
        others = sorted({r.get("engine") for r in data["results"] if r.get("engine")})
        reasons.insert(0, f"unknown to this SearXNG; answered by {', '.join(others)}")
    return mine, time.monotonic() - started, reasons[0] if reasons else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("ARGUS_SEARXNG_URL", "http://127.0.0.1:8888"))
    ap.add_argument("--engines", default=os.getenv("ARGUS_SEARCH_ENGINES") or DEFAULT_ENGINES)
    ap.add_argument(
        "--queries", type=int, default=3,
        help=f"probes per engine (max {len(QUERIES)})",
    )
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    rounds = QUERIES[: max(1, min(args.queries, len(QUERIES)))]
    print(f"backend {args.url}  |  {len(rounds)} queries per engine\n")
    print(f"{'engine':<14}{'ok':>6}{'results':>10}{'median':>9}  reason")

    failed = []
    for engine in engines:
        probes = [probe(args.url, engine, q, args.timeout) for q in rounds]
        ok = [p for p in probes if p[0] > 0]
        times = sorted(p[1] for p in probes)
        reason = next((p[2] for p in probes if p[2]), "")
        results = sum(p[0] for p in probes) // len(probes)
        print(
            f"{engine:<14}{len(ok)}/{len(probes):<4}{results:>10}"
            f"{times[len(times) // 2]:>8.1f}s  {reason}"
        )
        if not ok:
            failed.append(engine)

    if failed:
        print(f"\nno results at all from: {', '.join(failed)}")
        print("drop them from ARGUS_SEARCH_ENGINES, or route them through a proxy whose")
        print("exit IPs they accept (see docker-compose.proxy.yml).")
        return 1
    print("\nevery engine answered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
