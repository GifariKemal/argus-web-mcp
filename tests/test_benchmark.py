"""Unit tests for benchmark/scorer.py.

`benchmark` is not a package (no __init__), so we put it on sys.path and import
the module directly - mirrors how run_bench.py loads it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmark"))

import scorer  # noqa: E402


def test_lcs_len():
    assert scorer.lcs_len([], [1, 2]) == 0
    assert scorer.lcs_len(["a", "b", "c"], ["a", "c"]) == 2


def test_rouge_l_identical():
    assert scorer.rouge_l("a b c d", "a b c d") == 1.0


def test_rouge_l_empty():
    assert scorer.rouge_l("", "x") == 0.0
    assert scorer.rouge_l("x", "") == 0.0


def test_token_f1():
    assert scorer.token_f1("a b c d", "a b c d") == 1.0
    assert scorer.token_f1("d c b a", "a b c d") == 1.0  # order-insensitive
    assert scorer.token_f1("", "x") == 0.0


def test_truncation_completeness():
    assert abs(scorer.truncation_completeness("a b", "a b c d") - 0.5) < 1e-9
    assert scorer.truncation_completeness("a b c d e", "a b") == 1.0  # capped
    assert scorer.truncation_completeness("x", "") == 1.0  # empty gold


def test_success():
    assert scorer.success({"content": "hi"}) is True
    assert scorer.success({"content": ""}) is False
    assert scorer.success({"error": "x", "code": "fetch_failed"}) is False
    assert scorer.success("nope") is False


def test_score_item_reference_free():
    s = scorer.score_item("a b c", None, ok=True, latency=0.1)
    assert s["rouge_l"] is None and s["truncation"] is None and s["ok"] is True
