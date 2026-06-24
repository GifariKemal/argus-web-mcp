"""Offline unit tests for the pure functions in benchmark/semantic_ab.py.

No network, no LLM, no embedder - exercises only the I/O-free math (dcg, ndcg_at_k,
jaccard) and the judge-JSON parser, so the benchmark's scoring is trustworthy before
any live run. Known relevance lists -> known nDCG; perfect order nDCG == 1.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import semantic_ab as ab  # noqa: E402


def test_dcg_known_value():
    # rels [3, 2, 1]: 3/log2(2) + 2/log2(3) + 1/log2(4) = 3 + 1.26186 + 0.5 = 4.76186
    expected = 3 / math.log2(2) + 2 / math.log2(3) + 1 / math.log2(4)
    assert math.isclose(ab.dcg([3, 2, 1]), expected, rel_tol=1e-9)
    assert ab.dcg([]) == 0.0


def test_ndcg_perfect_order_is_one():
    # Already in ideal (descending) order -> nDCG == 1.0.
    assert math.isclose(ab.ndcg_at_k([3, 2, 1, 0], [3, 2, 1, 0], 5), 1.0, rel_tol=1e-12)


def test_ndcg_suboptimal_and_empty_pool():
    # Worst order of {3,2,1}: ranked [1,2,3] vs ideal [3,2,1].
    ranked = ab.dcg([1, 2, 3])
    ideal = ab.dcg([3, 2, 1])
    assert math.isclose(ab.ndcg_at_k([1, 2, 3], [3, 2, 1], 5), ranked / ideal, rel_tol=1e-12)
    # All-irrelevant pool -> IDCG 0 -> guarded to 0.0 (not NaN).
    assert ab.ndcg_at_k([0, 0, 0], [0, 0, 0], 5) == 0.0


def test_ndcg_truncates_at_k():
    # k=2 only counts the first two ranked rels; ideal also truncated to 2.
    val = ab.ndcg_at_k([2, 3, 3], [3, 3, 2], 2)
    assert math.isclose(val, ab.dcg([2, 3]) / ab.dcg([3, 3]), rel_tol=1e-12)


def test_jaccard():
    assert ab.jaccard(["a", "b", "c"], ["a", "b", "c"]) == 1.0  # identical
    assert ab.jaccard(["a", "b"], ["c", "d"]) == 0.0  # disjoint
    assert ab.jaccard(["a", "b"], ["b", "c"]) == 1 / 3  # one shared of three
    assert ab.jaccard([], []) == 1.0  # both empty -> degenerate identical


def test_parse_judge_json_variants():
    # bare list, clamped + in-range
    parsed = ab.parse_judge_json('[{"index":0,"score":3},{"index":1,"score":2}]', 2)
    assert parsed == {0: 3.0, 1: 2.0}
    # fenced + wrapper key, out-of-range index dropped, score clamped to 3
    fenced = '```json\n{"scores":[{"index":0,"score":9},{"index":5,"score":1}]}\n```'
    assert ab.parse_judge_json(fenced, 2) == {0: 3.0}
    # garbage / non-json -> empty dict, never raises
    assert ab.parse_judge_json("not json at all", 3) == {}


def test_is_conceptual():
    assert ab.is_conceptual("how to flash ESP32 over OTA")
    assert ab.is_conceptual("what is the actor model")
    assert ab.is_conceptual("git merge vs rebase")
    assert ab.is_conceptual("rust borrow checker explained")
    assert not ab.is_conceptual("ESP32 deep sleep current consumption microamps")


def test_rels_for_ordering_defaults_missing_to_zero():
    ordering = [{"url": "u1"}, {"url": "u2"}, {"url": "u3"}]
    judged = {"u1": 3.0, "u3": 1.0}  # u2 unjudged
    assert ab.rels_for_ordering(ordering, judged) == [3.0, 0.0, 1.0]
