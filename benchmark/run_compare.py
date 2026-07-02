"""Argus large-scale search benchmark - 3-arm comparison harness.

Three arms, one goal: surface Argus bugs / findings / areas-to-improve.

  1. ``argus``          - Argus ``search(q, count=10)`` over all 160 SCENARIOS,
                          paced to survive SearXNG per-IP throttling.
  2. ``argus-research`` - the real ``argus.research.research`` over the 40
                          COMPARE_IDS (full fetch+extract of top sources).
  3. (external) Claude WebSearch + Codex CLI over the same 40 - fed in as files,
     merged into the report by ``merge-3way``.

``score`` aggregates the 160-scenario sweep into metrics + a markdown report and
auto-flags weak categories. ``merge-3way`` builds the n=40 three-way section.

All aggregation/scoring logic is factored into PURE functions (no I/O) so it is
unit-tested offline in tests/test_compare_scorer.py.

Usage:
  python benchmark/run_compare.py argus --out out.json [--pace 4.0] [--limit N] [--ids a,b]
  python benchmark/run_compare.py argus-research --out r.json --ids-from-compare [--pace 4.0]
  python benchmark/run_compare.py score --argus out.json [--out report.md]
  python benchmark/run_compare.py merge-3way --argus-research r.json --claude c.json \\
      --codex-dir benchmark/codex_25 [--out report.md]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
SRC_DIR = BENCH_DIR.parent / "src"
for p in (str(BENCH_DIR), str(SRC_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scenarios as scen_mod  # noqa: E402

DEFAULT_REPORT = BENCH_DIR / "compare-report.md"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_URL_RE = re.compile(r"https?://[^\s<>()\]]+", re.IGNORECASE)


# --- pure scoring helpers (unit-tested, no I/O) ------------------------------


def query_tokens(text: str) -> set[str]:
    """Distinct lowercased word-tokens >=2 chars, hyphen/punctuation split.

    Matches argus.search tokenization so the relevance proxy is comparable.
    """
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2}


def title_overlap(query: str, title: str) -> float:
    """Fraction of distinct query tokens present in `title` (0..1).

    0.0 when the query has no usable tokens or the title is empty.
    """
    qtok = query_tokens(query)
    if not qtok:
        return 0.0
    ttok = query_tokens(title or "")
    return len(qtok & ttok) / len(qtok)


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _quantile(values: list[float], q: float) -> float:
    """p50/p95 helper. Returns 0.0 for empty; exact for tiny lists."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 3)
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return round(s[lo] + (s[hi] - s[lo]) * frac, 3)


def aggregate(records: list[dict]) -> dict:
    """Compute overall metrics for a list of argus-search records (pure)."""
    n = len(records)
    if n == 0:
        return {"n": 0}
    ok = [r for r in records if r.get("ok")]
    throttled = [r for r in records if r.get("throttled")]
    degraded = [r for r in ok if r.get("degraded")]
    no_results = [
        r for r in records if not r.get("ok") and r.get("code") == "no_results"
    ]
    other_err = [
        r
        for r in records
        if not r.get("ok") and not r.get("throttled") and r.get("code") != "no_results"
    ]
    latencies = [r["latency_s"] for r in records if r.get("latency_s") is not None]
    counts = [r.get("result_count", 0) for r in ok]
    overlaps = [r.get("top1_title_overlap", 0.0) for r in ok]
    return {
        "n": n,
        "success_pct": _pct(len(ok), n),
        "degraded_pct": _pct(len(degraded), n),
        "throttle_pct": _pct(len(throttled), n),
        "no_results_pct": _pct(len(no_results), n),
        "error_pct": _pct(len(other_err), n),
        "latency_p50": _quantile(latencies, 0.50),
        "latency_p95": _quantile(latencies, 0.95),
        "mean_result_count": round(statistics.mean(counts), 2) if counts else 0.0,
        "mean_top1_overlap": round(statistics.mean(overlaps), 3) if overlaps else 0.0,
    }


def aggregate_by_category(records: list[dict]) -> dict[str, dict]:
    """Per-category aggregate dicts, keyed by category (pure)."""
    cats: dict[str, list[dict]] = {}
    for r in records:
        cats.setdefault(r.get("category", "?"), []).append(r)
    return {c: aggregate(rs) for c, rs in sorted(cats.items())}


def engine_distribution(records: list[dict]) -> dict[str, int]:
    """Count of scenarios in which each engine appeared at least once (pure)."""
    dist: dict[str, int] = {}
    for r in records:
        for eng in set(r.get("engines") or []):
            dist[eng] = dist.get(eng, 0) + 1
    return dict(sorted(dist.items(), key=lambda kv: (-kv[1], kv[0])))


# Thresholds for the auto-flag "AREA TO IMPROVE" findings.
MIN_SUCCESS_PCT = 80.0
MIN_MEAN_OVERLAP = 0.30
MAX_THROTTLE_PCT = 30.0
MAX_DEGRADED_PCT = 20.0


def flag_findings(by_cat: dict[str, dict]) -> list[dict]:
    """Flag categories breaching a threshold as AREA TO IMPROVE (pure).

    Returns a list of {category, reasons:[...], metrics:{...}}.
    """
    findings: list[dict] = []
    for cat, agg in by_cat.items():
        reasons: list[str] = []
        if agg.get("success_pct", 100.0) < MIN_SUCCESS_PCT:
            reasons.append(f"success {agg['success_pct']}% < {MIN_SUCCESS_PCT}%")
        if agg.get("mean_top1_overlap", 1.0) < MIN_MEAN_OVERLAP:
            reasons.append(
                f"mean_top1_overlap {agg['mean_top1_overlap']} < {MIN_MEAN_OVERLAP}"
            )
        if agg.get("throttle_pct", 0.0) > MAX_THROTTLE_PCT:
            reasons.append(f"throttle {agg['throttle_pct']}% > {MAX_THROTTLE_PCT}%")
        if agg.get("degraded_pct", 0.0) > MAX_DEGRADED_PCT:
            reasons.append(f"degraded {agg['degraded_pct']}% > {MAX_DEGRADED_PCT}%")
        if reasons:
            findings.append({"category": cat, "reasons": reasons, "metrics": agg})
    return findings


def worst_scenarios(records: list[dict], n: int = 10) -> list[dict]:
    """The n weakest scenarios for manual review (pure).

    Rank: errored/throttled/no-result first, then lowest top1_title_overlap,
    then lowest result_count. Stable on id.
    """

    def key(r: dict) -> tuple:
        ok = bool(r.get("ok"))
        degraded = bool(r.get("degraded"))
        return (
            ok,  # False (failed) sorts first
            not degraded,  # degraded successes before healthy successes
            r.get("top1_title_overlap", 0.0) if ok else -1.0,
            r.get("result_count", 0) if ok else -1,
            r.get("id", ""),
        )

    return sorted(records, key=key)[:n]


def codex_found(text: str) -> bool:
    """Parse a Codex answer file body: found = non-empty AND contains a URL."""
    if not text or not text.strip():
        return False
    return bool(_URL_RE.search(text))


def codex_url_count(text: str) -> int:
    """Distinct URLs in a Codex answer (breadth proxy, pure)."""
    return len({m.group(0).rstrip(".,);") for m in _URL_RE.finditer(text or "")})


# --- async runners (I/O) -----------------------------------------------------


def _write_json(path: str, data: object) -> None:
    """Sync one-shot JSON dump (kept out of async bodies to avoid blocking-call lint)."""
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _select_scenarios(args) -> list[dict]:
    items = scen_mod.SCENARIOS
    if getattr(args, "ids", None):
        wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
        by = {s["id"]: s for s in items}
        items = [by[i] for i in wanted if i in by]
        missing = [i for i in wanted if i not in by]
        if missing:
            print(f"[warn] unknown ids skipped: {missing}", file=sys.stderr)
    if getattr(args, "limit", None):
        if args.limit < len(items):
            print(
                f"[cap] limiting {len(items)} -> {args.limit} scenarios (--limit)",
                file=sys.stderr,
            )
        items = items[: args.limit]
    return items


async def run_argus(args) -> None:
    import httpx

    from argus.search import SearchError
    from argus.search import search as argus_search

    # NOTE: use a PLAIN httpx client here, NOT build_safe_async_client. The SSRF
    # guard (correctly) refuses loopback IPs, but the SearXNG backend at
    # 127.0.0.1:8888 is trusted infrastructure - argus.search.search() itself
    # talks to it with a plain client. The SSRF client is for fetching arbitrary
    # *web pages* (the research arm), not the search backend.
    items = _select_scenarios(args)
    records: list[dict] = []
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=2))
    try:
        try:
            preflight = await client.get(
                "http://127.0.0.1:8888/search",
                params={"q": "argus preflight", "format": "json", "engines": "bing"},
                timeout=httpx.Timeout(10, connect=2),
            )
            preflight.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - benchmark infra gate, not app logic
            raise SystemExit(
                "SearXNG preflight failed at http://127.0.0.1:8888. "
                "Start deploy/searxng first (`docker compose up -d`) before running "
                f"the live Argus search benchmark. Cause: {type(exc).__name__}: {exc}"
            ) from exc

        for i, s in enumerate(items):
            # Deterministic pacing + index-derived jitter (no RNG) to dodge per-IP throttle.
            if i > 0:
                await asyncio.sleep(args.pace + (i % 5) * 0.5)
            rec = {
                "id": s["id"],
                "category": s["category"],
                "query": s["query"],
                "ok": False,
                "code": None,
                "result_count": 0,
                "latency_s": None,
                "engines": [],
                "top1_title_overlap": 0.0,
                "throttled": False,
                "degraded": False,
                "degraded_reason": None,
            }
            t0 = time.perf_counter()
            try:
                res = await argus_search(s["query"], count=10, client=client)
                rec["latency_s"] = round(time.perf_counter() - t0, 3)
                results = res.get("results", [])
                rec["result_count"] = len(results)
                rec["engines"] = res.get("engines_used", [])
                rec["ok"] = len(results) >= 1
                rec["degraded"] = bool(res.get("degraded"))
                rec["degraded_reason"] = res.get("degraded_reason")
                if results:
                    rec["top1_title_overlap"] = round(
                        title_overlap(s["query"], results[0].get("title", "")), 3
                    )
            except SearchError as e:
                rec["latency_s"] = round(time.perf_counter() - t0, 3)
                rec["code"] = e.code
                rec["throttled"] = e.code == "search_backend_down"
            except Exception as e:  # noqa: BLE001 - record, never crash the sweep
                rec["latency_s"] = round(time.perf_counter() - t0, 3)
                rec["code"] = f"unexpected:{type(e).__name__}"
            records.append(rec)
            _write_json(args.out, records)  # resume/debug friendly; survives interrupts
            if (i + 1) % 20 == 0:
                done = sum(1 for r in records if r["ok"])
                print(f"  [{i + 1}/{len(items)}] ok={done}", file=sys.stderr)
    finally:
        await client.aclose()

    _write_json(args.out, records)
    print(f"wrote {len(records)} argus records -> {args.out}")


async def run_argus_research(args) -> None:
    from argus.fetch.render import BrowserPool
    from argus.research import research
    from argus.search import SearchError
    from argus.security.ssrf import build_safe_async_client

    ids = scen_mod.COMPARE_IDS
    items = [scen_mod.by_id(i) for i in ids]
    records: list[dict] = []
    client = build_safe_async_client(timeout=30)
    browser = BrowserPool()
    await browser.start()
    try:
        for i, s in enumerate(items):
            if i > 0:
                await asyncio.sleep(args.pace + (i % 5) * 0.5)
            t0 = time.perf_counter()
            rec: dict = {"id": s["id"], "query": s["query"]}
            try:
                bundle = await research(
                    s["query"], client=client, browser=browser
                )
                rec["latency_s"] = round(time.perf_counter() - t0, 3)
                srcs = bundle.get("sources", [])
                rec["count"] = bundle.get("count", len(srcs))
                rec["failed"] = len(bundle.get("failed", []))
                rec["total_words"] = sum(x.get("word_count", 0) for x in srcs)
                rec["sources"] = [
                    {
                        "url": x.get("url"),
                        "title": x.get("title"),
                        "word_count": x.get("word_count", 0),
                        "render_path": x.get("render_path"),
                    }
                    for x in srcs
                ]
            except SearchError as e:
                rec["latency_s"] = round(time.perf_counter() - t0, 3)
                rec["error"] = e.code
                rec["count"] = 0
                rec["failed"] = 0
                rec["total_words"] = 0
                rec["sources"] = []
            except Exception as e:  # noqa: BLE001 - isolate per-scenario failure
                rec["latency_s"] = round(time.perf_counter() - t0, 3)
                rec["error"] = f"unexpected:{type(e).__name__}"
                rec["count"] = 0
                rec["failed"] = 0
                rec["total_words"] = 0
                rec["sources"] = []
            records.append(rec)
            print(
                f"  [{i + 1}/{len(items)}] {s['id']} "
                f"count={rec.get('count')} words={rec.get('total_words')}",
                file=sys.stderr,
            )
    finally:
        await browser.stop()
        await client.aclose()

    _write_json(args.out, records)
    print(f"wrote {len(records)} argus-research records -> {args.out}")


# --- report rendering --------------------------------------------------------


def _overall_table(agg: dict) -> str:
    return (
        "| metric | value |\n|---|---|\n"
        f"| scenarios | {agg.get('n', 0)} |\n"
        f"| success % | {agg.get('success_pct', 0)} |\n"
        f"| degraded % | {agg.get('degraded_pct', 0)} |\n"
        f"| throttle % | {agg.get('throttle_pct', 0)} |\n"
        f"| no_results % | {agg.get('no_results_pct', 0)} |\n"
        f"| other-error % | {agg.get('error_pct', 0)} |\n"
        f"| latency p50 (s) | {agg.get('latency_p50', 0)} |\n"
        f"| latency p95 (s) | {agg.get('latency_p95', 0)} |\n"
        f"| mean result_count | {agg.get('mean_result_count', 0)} |\n"
        f"| mean top1_title_overlap | {agg.get('mean_top1_overlap', 0)} |\n"
    )


def _per_category_table(by_cat: dict[str, dict]) -> str:
    head = (
        "| category | n | success% | degraded% | throttle% | no_res% | err% | "
        "p50 | p95 | mean#res | mean_overlap |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for cat, a in by_cat.items():
        rows.append(
            f"| {cat} | {a['n']} | {a['success_pct']} | {a.get('degraded_pct', 0)} | "
            f"{a['throttle_pct']} | {a['no_results_pct']} | {a['error_pct']} | "
            f"{a['latency_p50']} | "
            f"{a['latency_p95']} | {a['mean_result_count']} | {a['mean_top1_overlap']} |"
        )
    return head + "\n".join(rows) + "\n"


def render_report(records: list[dict]) -> str:
    agg = aggregate(records)
    by_cat = aggregate_by_category(records)
    engines = engine_distribution(records)
    findings = flag_findings(by_cat)
    worst = worst_scenarios(records, 10)

    parts: list[str] = []
    parts.append("# Argus search benchmark - report\n")
    parts.append(f"_Argus arm: {agg.get('n', 0)} scenarios (SearXNG `search`, count=10)._\n")

    parts.append("\n## Overall metrics\n")
    parts.append(_overall_table(agg))

    parts.append("\n## Per-category\n")
    parts.append(_per_category_table(by_cat))

    parts.append("\n## Engine answer distribution\n")
    parts.append("(scenarios in which each engine contributed >=1 top result)\n\n")
    parts.append("| engine | scenarios |\n|---|---|\n")
    parts.append(
        "\n".join(f"| {e} | {c} |" for e, c in engines.items()) + "\n"
        if engines
        else "_(none)_\n"
    )

    parts.append("\n## Findings\n")
    if findings:
        parts.append(
            f"Thresholds: success >= {MIN_SUCCESS_PCT}%, "
            f"mean_top1_overlap >= {MIN_MEAN_OVERLAP}, throttle <= {MAX_THROTTLE_PCT}%, "
            f"degraded <= {MAX_DEGRADED_PCT}%.\n\n"
        )
        for f in findings:
            parts.append(f"- **AREA TO IMPROVE - {f['category']}**: {'; '.join(f['reasons'])}\n")
    else:
        parts.append("No category breached the AREA-TO-IMPROVE thresholds.\n")

    parts.append("\n### 10 worst scenarios (manual review)\n")
    parts.append("| id | category | ok | degraded | overlap | #res | code | query |\n")
    parts.append("|---|---|---|---|---|---|---|---|\n")
    for r in worst:
        q = r.get("query", "")
        q = (q[:57] + "...") if len(q) > 60 else q
        parts.append(
            f"| {r.get('id')} | {r.get('category')} | {r.get('ok')} | "
            f"{r.get('degraded', False)} | {r.get('top1_title_overlap', 0.0)} | "
            f"{r.get('result_count', 0)} | "
            f"{r.get('code')} | {q} |\n"
        )
    return "".join(parts)


def run_score(args) -> None:
    records = json.loads(Path(args.argus).read_text(encoding="utf-8"))
    print(f"[info] scoring {len(records)} argus records from {args.argus}")
    report = render_report(records)
    out = Path(args.out) if args.out else DEFAULT_REPORT
    out.write_text(report, encoding="utf-8")
    agg = aggregate(records)
    print(
        f"success={agg.get('success_pct')}% throttle={agg.get('throttle_pct')}% "
        f"mean_overlap={agg.get('mean_top1_overlap')} -> {out}"
    )


def build_3way_rows(
    research_recs: list[dict],
    claude_recs: list[dict],
    codex_texts: dict[str, str],
) -> list[dict]:
    """Combine the three arms per COMPARE_ID into comparable rows (pure).

    Returns rows {id, query, argus_found, argus_breadth, argus_words,
    claude_found, claude_breadth, codex_found, codex_breadth}.
    """
    research_by = {r["id"]: r for r in research_recs}
    claude_by = {c["id"]: c for c in claude_recs}
    rows: list[dict] = []
    for sid in scen_mod.COMPARE_IDS:
        rr = research_by.get(sid, {})
        cl = claude_by.get(sid, {})
        cx = codex_texts.get(sid, "")
        rows.append(
            {
                "id": sid,
                "query": rr.get("query") or scen_mod.by_id(sid)["query"],
                "argus_found": rr.get("count", 0) >= 1,
                "argus_breadth": rr.get("count", 0),
                "argus_words": rr.get("total_words", 0),
                "claude_found": bool(cl.get("found")),
                "claude_breadth": cl.get("result_count", 0),
                "codex_found": codex_found(cx),
                "codex_breadth": codex_url_count(cx),
            }
        )
    return rows


def tally_3way(rows: list[dict]) -> dict:
    """Verdict tally across the three arms (pure)."""
    n = len(rows)
    return {
        "n": n,
        "argus_found": sum(1 for r in rows if r["argus_found"]),
        "claude_found": sum(1 for r in rows if r["claude_found"]),
        "codex_found": sum(1 for r in rows if r["codex_found"]),
        "argus_mean_breadth": round(
            statistics.mean([r["argus_breadth"] for r in rows]), 2
        )
        if n
        else 0.0,
        "claude_mean_breadth": round(
            statistics.mean([r["claude_breadth"] for r in rows]), 2
        )
        if n
        else 0.0,
        "codex_mean_breadth": round(
            statistics.mean([r["codex_breadth"] for r in rows]), 2
        )
        if n
        else 0.0,
        "argus_mean_words": round(statistics.mean([r["argus_words"] for r in rows]), 0)
        if n
        else 0.0,
    }


def render_3way_section(rows: list[dict], tally: dict) -> str:
    parts: list[str] = []
    parts.append(f"\n## 3-way (n={tally['n']})\n")
    parts.append("Argus research (full read) vs Claude WebSearch vs Codex CLI.\n\n")
    parts.append(
        "| id | A:found | A:#src | A:words | C:found | C:#res | "
        "X:found | X:#urls | query |\n"
    )
    parts.append("|---|---|---|---|---|---|---|---|---|\n")
    for r in rows:
        q = r["query"]
        q = (q[:47] + "...") if len(q) > 50 else q
        parts.append(
            f"| {r['id']} | {r['argus_found']} | {r['argus_breadth']} | {r['argus_words']} | "
            f"{r['claude_found']} | {r['claude_breadth']} | {r['codex_found']} | "
            f"{r['codex_breadth']} | {q} |\n"
        )
    parts.append("\n### Verdict tally\n")
    parts.append("| arm | found (of n) | mean breadth |\n|---|---|---|\n")
    parts.append(
        f"| Argus | {tally['argus_found']}/{tally['n']} | {tally['argus_mean_breadth']} |\n"
    )
    parts.append(
        f"| Claude WebSearch | {tally['claude_found']}/{tally['n']} | "
        f"{tally['claude_mean_breadth']} |\n"
    )
    parts.append(
        f"| Codex CLI | {tally['codex_found']}/{tally['n']} | {tally['codex_mean_breadth']} |\n"
    )
    parts.append(f"\nArgus mean full-content words/scenario: **{tally['argus_mean_words']}** ")
    parts.append("(depth advantage - competitors return search hits / summaries, not full text).\n")
    return "".join(parts)


def run_merge_3way(args) -> None:
    research_recs = json.loads(Path(args.argus_research).read_text(encoding="utf-8"))
    claude_recs = json.loads(Path(args.claude).read_text(encoding="utf-8"))
    codex_dir = Path(args.codex_dir)
    codex_texts: dict[str, str] = {}
    for sid in scen_mod.COMPARE_IDS:
        f = codex_dir / f"{sid}.txt"
        if f.is_file():
            codex_texts[sid] = f.read_text(encoding="utf-8", errors="replace")
        else:
            print(f"[warn] missing codex output: {f}", file=sys.stderr)
    rows = build_3way_rows(research_recs, claude_recs, codex_texts)
    tally = tally_3way(rows)
    section = render_3way_section(rows, tally)

    out = Path(args.out) if args.out else DEFAULT_REPORT
    if out.is_file():
        existing = out.read_text(encoding="utf-8")
        # Replace a prior 3-way section if present, else append.
        marker = "\n## 3-way (n="
        if marker in existing:
            existing = existing[: existing.index(marker)]
        out.write_text(existing.rstrip() + "\n" + section, encoding="utf-8")
    else:
        out.write_text("# Argus 3-way comparison\n" + section, encoding="utf-8")
    print(
        f"3-way: argus={tally['argus_found']} claude={tally['claude_found']} "
        f"codex={tally['codex_found']} (of {tally['n']}) -> {out}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Argus 3-arm search benchmark harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("argus", help="run Argus search over scenarios (default: all 160)")
    a.add_argument("--out", required=True)
    a.add_argument("--pace", type=float, default=4.0, help="base seconds between calls")
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--ids", default=None, help="comma-separated scenario ids")

    r = sub.add_parser("argus-research", help="run argus.research over the 40 COMPARE_IDS")
    r.add_argument("--out", required=True)
    r.add_argument("--ids-from-compare", action="store_true", default=True)
    r.add_argument("--pace", type=float, default=4.0)

    s = sub.add_parser("score", help="aggregate the argus 160-run into a report")
    s.add_argument("--argus", required=True)
    s.add_argument("--out", default=None)

    m = sub.add_parser("merge-3way", help="merge the n=40 three-way arms into the report")
    m.add_argument("--argus-research", required=True)
    m.add_argument("--claude", required=True)
    m.add_argument("--codex-dir", required=True)
    m.add_argument("--out", default=None)

    args = p.parse_args()
    if args.cmd == "argus":
        asyncio.run(run_argus(args))
    elif args.cmd == "argus-research":
        asyncio.run(run_argus_research(args))
    elif args.cmd == "score":
        run_score(args)
    elif args.cmd == "merge-3way":
        run_merge_3way(args)


if __name__ == "__main__":
    main()
