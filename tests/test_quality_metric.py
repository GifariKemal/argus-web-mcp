"""Unit tests for the formatting-invariant extraction-quality metric in scorer.py.

The whole point of this metric is that it is FORMATTING-INVARIANT: markdown
headings/code-fences/bullets must not change the score; only "is the content in
and is the boilerplate out" matters. These tests pin that contract.

`benchmark` is not a package, so we put it on sys.path (mirrors run_bench.py).
"""

import sys
from pathlib import Path

import yaml

BENCH = Path(__file__).resolve().parent.parent / "benchmark"
sys.path.insert(0, str(BENCH))

import scorer  # noqa: E402

# --- _sentences -------------------------------------------------------------


def test_sentences_empty():
    assert scorer._sentences("") == []


def test_sentences_drops_short_units():
    assert scorer._sentences("too short") == []  # < 4 tokens


def test_sentences_normalizes_and_splits():
    out = scorer._sentences("This ONE has  enough tokens here. And another full clause too!")
    assert out == [
        "this one has enough tokens here",
        "and another full clause too",
    ]


def test_sentences_splits_on_newline():
    out = scorer._sentences("first clause has four words\nsecond clause has four words")
    assert out == ["first clause has four words", "second clause has four words"]


# --- content_recall ---------------------------------------------------------


def test_content_recall_empty_gold_is_one():
    assert scorer.content_recall("anything at all here", []) == 1.0


def test_content_recall_all_present():
    pred = "The quick brown fox jumps over the lazy sleeping dog every morning."
    must = ["the quick brown fox jumps", "over the lazy sleeping dog"]
    assert scorer.content_recall(pred, must) == 1.0


def test_content_recall_formatting_invariant():
    # markdown headings / code fences / list bullets must NOT affect the score
    md = "# Title\n\n```python\nThe quick brown fox jumps\n```\n\n- over the lazy sleeping dog\n"
    must = ["the quick brown fox jumps", "over the lazy sleeping dog"]
    assert scorer.content_recall(md, must) == 1.0


def test_content_recall_partial():
    pred = "The quick brown fox jumps over a totally different ending here."
    must = ["the quick brown fox jumps", "over the lazy sleeping dog"]
    assert scorer.content_recall(pred, must) == 0.5  # 1 of 2


def test_content_recall_high_overlap_tolerates_small_diff():
    # 0.8 threshold: dropping one of five snippet tokens still counts as present
    pred = "alpha beta gamma delta is present in the document body here"
    must = ["alpha beta gamma delta epsilon"]  # 4/5 = 0.8 overlap
    assert scorer.content_recall(pred, must) == 1.0


def test_content_recall_missing_is_zero():
    assert scorer.content_recall(
        "nothing relevant whatsoever here", ["completely absent phrase indeed"]
    ) == 0.0


# --- boilerplate_rejection --------------------------------------------------


def test_boilerplate_rejection_empty_is_one():
    assert scorer.boilerplate_rejection("anything", []) == 1.0


def test_boilerplate_rejection_all_absent():
    clean = "Just the article body, nothing else."
    boiler = ["Subscribe to our newsletter", "Edit on GitHub", "Accept all cookies"]
    assert scorer.boilerplate_rejection(clean, boiler) == 1.0


def test_boilerplate_rejection_case_insensitive_substring():
    pred = "body text SUBSCRIBE TO OUR NEWSLETTER more body"
    assert scorer.boilerplate_rejection(pred, ["Subscribe to our newsletter"]) == 0.0


def test_boilerplate_rejection_partial_leak():
    pred = "article body. Subscribe to our newsletter."
    boiler = ["Subscribe to our newsletter", "Edit on GitHub", "Accept all cookies"]
    assert abs(scorer.boilerplate_rejection(pred, boiler) - (2 / 3)) < 1e-9


# --- quality_f1 -------------------------------------------------------------


def test_quality_f1_perfect():
    pred = "The quick brown fox jumps over the lazy sleeping dog."
    must = ["the quick brown fox jumps", "over the lazy sleeping dog"]
    boiler = ["Subscribe", "Edit on GitHub"]
    assert scorer.quality_f1(pred, must, boiler) == 1.0


def test_quality_f1_raw_dump_keeps_content_but_leaks_boilerplate():
    # the confounded-gold problem made concrete: a raw dump has recall 1.0 but
    # leaks every boilerplate string -> rejection 0 -> f1 0. This is the behaviour
    # the fair metric must have (ROUGE-L would REWARD this dump instead).
    must = ["the quick brown fox jumps", "over the lazy sleeping dog"]
    boiler = ["Subscribe to our newsletter", "Edit on GitHub"]
    raw_dump = ("Subscribe to our newsletter. The quick brown fox jumps over the "
                "lazy sleeping dog. Edit on GitHub.")
    assert scorer.content_recall(raw_dump, must) == 1.0
    assert scorer.boilerplate_rejection(raw_dump, boiler) == 0.0
    assert scorer.quality_f1(raw_dump, must, boiler) == 0.0


def test_quality_f1_dropped_content_lowers_score():
    # an extractor that over-trims real body text loses on recall even if clean
    must = ["the quick brown fox jumps", "over the lazy sleeping dog"]
    boiler = ["Subscribe"]
    over_trimmed = "the quick brown fox jumps"  # dropped half the body
    r = scorer.content_recall(over_trimmed, must)
    assert r == 0.5
    assert scorer.boilerplate_rejection(over_trimmed, boiler) == 1.0
    # harmonic mean of 0.5 and 1.0 = 0.667
    assert abs(scorer.quality_f1(over_trimmed, must, boiler) - (2 / 3)) < 1e-9


def test_quality_f1_empty_lists():
    assert scorer.quality_f1("anything", [], []) == 1.0


# --- the curated gold file itself is well-formed ----------------------------


def test_quality_gold_yaml_is_valid():
    gold = yaml.safe_load((BENCH / "quality_gold.yaml").read_text(encoding="utf-8"))
    assert isinstance(gold, list) and len(gold) >= 2
    seen_ids = set()
    for item in gold:
        assert item["id"] not in seen_ids
        seen_ids.add(item["id"])
        assert item["url"].startswith("http")
        mc = item["must_contain"]
        mnc = item["must_not_contain"]
        assert isinstance(mc, list) and len(mc) >= 5  # 5-10 main-content phrases
        assert isinstance(mnc, list) and len(mnc) >= 1
        # snippets must be non-trivial (the metric drops < 4-token units anyway)
        assert all(len(s.split()) >= 4 for s in mc)
