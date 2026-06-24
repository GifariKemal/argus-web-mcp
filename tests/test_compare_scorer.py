"""Offline unit tests for the pure scorer/aggregate functions in run_compare.py.

No network, no Argus imports needed - exercises only the I/O-free helpers so the
benchmark math is trustworthy before the orchestrator runs the full sweep.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import run_compare as rc  # noqa: E402


def test_title_overlap_fraction():
    # 4 distinct query tokens {deep, sleep, power, draw}; title has deep+sleep => 2/4 = 0.5.
    assert rc.title_overlap("deep sleep power draw", "ESP deep sleep dive") == 0.5
    # Hyphen split: query "esp-claw" -> {esp, claw}; both in title => 1.0.
    assert rc.title_overlap("esp-claw guide", "the esp claw guide") == 1.0
    # No overlap => 0.0; empty/garbage query (<2 char tokens) => 0.0.
    assert rc.title_overlap("python asyncio", "rust tokio") == 0.0
    assert rc.title_overlap("a b", "anything here") == 0.0


def test_aggregate_overall():
    recs = [
        {"id": "x1", "category": "dev", "ok": True, "code": None,
         "result_count": 10, "latency_s": 1.0, "engines": ["bing"],
         "top1_title_overlap": 0.8, "throttled": False},
        {"id": "x2", "category": "dev", "ok": True, "code": None,
         "result_count": 6, "latency_s": 3.0, "engines": ["ddg"],
         "top1_title_overlap": 0.4, "throttled": False},
        {"id": "x3", "category": "dev", "ok": False, "code": "search_backend_down",
         "result_count": 0, "latency_s": 2.0, "engines": [],
         "top1_title_overlap": 0.0, "throttled": True},
        {"id": "x4", "category": "dev", "ok": False, "code": "no_results",
         "result_count": 0, "latency_s": 0.5, "engines": [],
         "top1_title_overlap": 0.0, "throttled": False},
    ]
    agg = rc.aggregate(recs)
    assert agg["n"] == 4
    assert agg["success_pct"] == 50.0
    assert agg["throttle_pct"] == 25.0
    assert agg["no_results_pct"] == 25.0
    assert agg["error_pct"] == 0.0
    # mean over the 2 ok records only.
    assert agg["mean_result_count"] == 8.0
    assert agg["mean_top1_overlap"] == 0.6


def test_flag_findings_thresholds():
    by_cat = {
        "good": {"n": 20, "success_pct": 95.0, "throttle_pct": 5.0,
                 "no_results_pct": 0.0, "error_pct": 0.0, "latency_p50": 1.0,
                 "latency_p95": 2.0, "mean_result_count": 9.0,
                 "mean_top1_overlap": 0.7},
        "weak": {"n": 20, "success_pct": 70.0, "throttle_pct": 40.0,
                 "no_results_pct": 10.0, "error_pct": 0.0, "latency_p50": 1.0,
                 "latency_p95": 2.0, "mean_result_count": 4.0,
                 "mean_top1_overlap": 0.2},
    }
    findings = rc.flag_findings(by_cat)
    flagged = {f["category"] for f in findings}
    assert flagged == {"weak"}  # only the breaching category is flagged
    weak = findings[0]
    # weak breaches all three thresholds (success, overlap, throttle).
    assert len(weak["reasons"]) == 3


def test_worst_scenarios_orders_failures_first():
    recs = [
        {"id": "a", "ok": True, "top1_title_overlap": 0.9, "result_count": 10},
        {"id": "b", "ok": False, "code": "no_results", "top1_title_overlap": 0.0,
         "result_count": 0},
        {"id": "c", "ok": True, "top1_title_overlap": 0.1, "result_count": 2},
    ]
    worst = rc.worst_scenarios(recs, 2)
    assert worst[0]["id"] == "b"  # failure first
    assert worst[1]["id"] == "c"  # then lowest-overlap success


def test_codex_found_and_url_count():
    assert rc.codex_found("Top source: https://docs.python.org/3/ asyncio") is True
    assert rc.codex_found("no links here, just prose") is False
    assert rc.codex_found("") is False
    text = "see https://a.com/x and https://b.org/y and https://a.com/x again"
    assert rc.codex_url_count(text) == 2  # distinct urls


def test_engine_distribution_counts_scenarios():
    recs = [
        {"id": "1", "engines": ["bing", "ddg"]},
        {"id": "2", "engines": ["bing"]},
        {"id": "3", "engines": []},
    ]
    dist = rc.engine_distribution(recs)
    assert dist["bing"] == 2
    assert dist["ddg"] == 1


def test_build_3way_rows_and_tally():
    research = [{"id": rc.scen_mod.COMPARE_IDS[0], "query": "q0",
                 "count": 4, "total_words": 5000}]
    claude = [{"id": rc.scen_mod.COMPARE_IDS[0], "found": True,
               "result_count": 6, "top_titles": [], "note": ""}]
    codex = {rc.scen_mod.COMPARE_IDS[0]: "src https://x.com/a https://y.com/b"}
    rows = rc.build_3way_rows(research, claude, codex)
    assert len(rows) == len(rc.scen_mod.COMPARE_IDS)  # one per COMPARE_ID, missing arms = not-found
    first = rows[0]
    assert first["argus_found"] is True
    assert first["argus_breadth"] == 4
    assert first["claude_found"] is True
    assert first["codex_found"] is True
    assert first["codex_breadth"] == 2
    tally = rc.tally_3way(rows)
    assert tally["n"] == len(rc.scen_mod.COMPARE_IDS)
    assert tally["argus_found"] == 1  # only the one populated record
    assert tally["claude_found"] == 1
    assert tally["codex_found"] == 1
