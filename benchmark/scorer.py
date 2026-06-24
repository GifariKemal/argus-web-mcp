"""Reference-based + reference-free scoring for the Argus benchmark.

Ponytail: stdlib only - no rouge/nltk/numpy. All metrics are token-level over
whitespace-split, lowercased tokens. ROUGE-L uses an LCS DP (O(n*m)); the gold
texts are bounded (curated main-content), so this is fine for the testset.

Run the self-check:  python benchmark/scorer.py
"""

from __future__ import annotations

import re
from collections import Counter


def _tokens(text: str) -> list[str]:
    return text.lower().split()


_WORD_RE = re.compile(r"[0-9a-z]+")


def _word_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric word-tokens, punctuation/markdown stripped.

    Used by the content-presence check so the score is genuinely formatting-
    invariant: markdown emphasis (``**OpenAPI**``), literal quotes (``"schema"``),
    heading anchors (````) and trailing punctuation all reduce to the same bare
    word as clean prose. (``boilerplate_rejection`` deliberately stays a raw
    substring check - it must catch cruft regardless of surrounding chars.)
    """
    return _WORD_RE.findall(text.lower())


def lcs_len(a: list, b: list) -> int:
    """Length of the longest common subsequence of two sequences (classic DP)."""
    if not a or not b:
        return 0
    # Rolling 1-D DP row to keep memory at O(min(len)).
    if len(a) < len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, start=1):
            cur[j] = prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1])
        prev = cur
    return prev[len(b)]


def rouge_l(pred: str, gold: str) -> float:
    """Token-level ROUGE-L F1 via LCS. 0.0 if either side is empty."""
    p, g = _tokens(pred), _tokens(gold)
    if not p or not g:
        return 0.0
    lcs = lcs_len(p, g)
    if lcs == 0:
        return 0.0
    recall = lcs / len(g)
    precision = lcs / len(p)
    return 2 * precision * recall / (precision + recall)


def token_f1(pred: str, gold: str) -> float:
    """Multiset (bag-of-tokens) overlap F1. 0.0 if either side is empty."""
    p, g = _tokens(pred), _tokens(gold)
    if not p or not g:
        return 0.0
    overlap = sum((Counter(p) & Counter(g)).values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(p)
    recall = overlap / len(g)
    return 2 * precision * recall / (precision + recall)


def _sentences(text: str) -> list[str]:
    """Split into normalized comparable units.

    Lowercase, collapse internal whitespace, split on sentence-ending punctuation
    and newlines, drop fragments of fewer than 4 tokens. Formatting-invariant: it
    does not care about markdown headings/fences/list bullets - only the words.
    """
    if not text:
        return []
    lowered = text.lower()
    # Split on newlines and sentence terminators (.!?). Keep it stdlib + simple.
    raw = re.split(r"[.!?\n]+", lowered)
    units: list[str] = []
    for chunk in raw:
        toks = chunk.split()
        if len(toks) >= 4:
            units.append(" ".join(toks))
    return units


def _snippet_present(pred_tokens: list[str], snippet: str, thresh: float = 0.8) -> bool:
    """True iff snippet's word-tokens appear as a high-overlap run in pred.

    `pred_tokens` must be word-tokens (see `_word_tokens`). We slide a window the
    size of the snippet across pred and accept if any window shares >= `thresh` of
    the snippet's tokens (as a multiset). Sliding-window + multiset overlap makes
    it robust to small reordering / dropped stop-tokens; word-tokenising both sides
    makes it indifferent to markdown wrapping - ``**OpenAPI**``, ``"schema"`` and a
    bare ``openapi`` / ``schema`` all match identically.
    """
    snip = _word_tokens(snippet)
    if not snip:
        return True
    n = len(snip)
    need = thresh * n
    snip_counter = Counter(snip)
    if len(pred_tokens) < n:
        # Whole pred is shorter than the snippet - best possible overlap is all of pred.
        overlap = sum((Counter(pred_tokens) & snip_counter).values())
        return overlap >= need
    for i in range(len(pred_tokens) - n + 1):
        window = pred_tokens[i : i + n]
        overlap = sum((Counter(window) & snip_counter).values())
        if overlap >= need:
            return True
    return False


def content_recall(pred: str, must_contain: list[str]) -> float:
    """Fraction of `must_contain` gold snippets present in pred.

    A snippet is 'present' if its normalized tokens appear as a high-overlap run in
    pred (>=0.8 token-overlap of the snippet). Formatting-invariant: markdown
    headings/code fences don't matter, only whether the content text is there.
    Returns 1.0 if must_contain is empty.
    """
    if not must_contain:
        return 1.0
    pred_tokens = _word_tokens(pred)
    hit = sum(1 for s in must_contain if _snippet_present(pred_tokens, s))
    return hit / len(must_contain)


def boilerplate_rejection(pred: str, must_not_contain: list[str]) -> float:
    """Fraction of known-boilerplate strings ABSENT from pred (case-insensitive substring).

    Returns 1.0 if must_not_contain is empty. A raw DOM dump that keeps nav/ads
    scores LOW here because those boilerplate strings survive into the output.
    """
    if not must_not_contain:
        return 1.0
    low = pred.lower()
    absent = sum(1 for s in must_not_contain if s.lower() not in low)
    return absent / len(must_not_contain)


def quality_f1(pred: str, must_contain: list[str], must_not_contain: list[str]) -> float:
    """Harmonic mean of content_recall and boilerplate_rejection.

    Captures "got the main content in AND left the boilerplate out" in one number.
    Formatting-invariant - favours no adapter by construction.
    """
    r = content_recall(pred, must_contain)
    b = boilerplate_rejection(pred, must_not_contain)
    if r + b == 0:
        return 0.0
    return 2 * r * b / (r + b)


def truncation_completeness(pred: str, gold: str) -> float:
    """How much of gold's length pred covers: min(1.0, |pred|/|gold|). 1.0 if gold empty."""
    g = _tokens(gold)
    if not g:
        return 1.0
    return min(1.0, len(_tokens(pred)) / len(g))


def success(result: dict) -> bool:
    """True iff result is a non-error dict with non-empty content."""
    if not isinstance(result, dict):
        return False
    if result.get("error") or result.get("code"):
        return False
    return bool(result.get("content"))


def score_item(
    pred_content: str, gold_content: str | None, ok: bool, latency: float
) -> dict:
    """All metrics for one (prediction, gold) pair.

    Gold-based metrics are None in reference-free mode (gold_content is None).
    """
    out: dict = {
        "ok": ok,
        "latency": latency,
        "pred_words": len(_tokens(pred_content)),
        "rouge_l": None,
        "token_f1": None,
        "truncation": None,
    }
    if gold_content is not None:
        out["rouge_l"] = rouge_l(pred_content, gold_content)
        out["token_f1"] = token_f1(pred_content, gold_content)
        out["truncation"] = truncation_completeness(pred_content, gold_content)
    return out


if __name__ == "__main__":
    # --- assert-based self-check (Ponytail: no test framework needed to smoke it) ---
    assert lcs_len([], [1, 2]) == 0
    assert lcs_len(["a", "b", "c"], ["a", "c"]) == 2
    assert lcs_len(["a", "b", "c", "d"], ["b", "d", "a"]) == 2  # "b","d"

    assert rouge_l("a b c d", "a b c d") == 1.0
    assert rouge_l("", "x") == 0.0
    assert rouge_l("x", "") == 0.0
    assert rouge_l("totally different words", "nothing shared here ok") == 0.0
    # half the gold tokens recalled, all pred tokens matched -> P=1,R=0.5,F=0.667
    assert abs(rouge_l("a b", "a b c d") - (2 * 1.0 * 0.5 / 1.5)) < 1e-9

    assert token_f1("a b c d", "a b c d") == 1.0
    assert token_f1("", "x") == 0.0
    # order-insensitive (bag of tokens)
    assert token_f1("d c b a", "a b c d") == 1.0

    assert truncation_completeness("a b c d", "a b c d") == 1.0
    assert abs(truncation_completeness("a b", "a b c d") - 0.5) < 1e-9
    assert truncation_completeness("a b c d e f", "a b") == 1.0  # capped at 1.0
    assert truncation_completeness("anything", "") == 1.0  # empty gold

    assert success({"content": "hi"}) is True
    assert success({"content": ""}) is False
    assert success({"error": "x", "code": "fetch_failed"}) is False
    assert success("not a dict") is False

    s = score_item("a b c", "a b c d", ok=True, latency=0.5)
    assert s["ok"] is True and s["rouge_l"] is not None
    s2 = score_item("a b c", None, ok=True, latency=0.5)
    assert s2["rouge_l"] is None and s2["truncation"] is None

    # --- formatting-invariant extraction-quality metric ---
    assert _sentences("") == []
    assert _sentences("too short") == []  # < 4 tokens dropped
    assert _sentences("this one has enough tokens here. and another full clause too") == [
        "this one has enough tokens here",
        "and another full clause too",
    ]

    must = ["the quick brown fox jumps", "over the lazy sleeping dog"]
    boiler = ["Subscribe to our newsletter", "Edit on GitHub", "Accept all cookies"]

    # clean pred: all content present, no boilerplate -> recall 1, rejection 1, f1 1
    clean = "The quick brown fox jumps over the lazy sleeping dog every morning."
    assert content_recall(clean, must) == 1.0
    assert boilerplate_rejection(clean, boiler) == 1.0
    assert quality_f1(clean, must, boiler) == 1.0

    # markdown formatting must not matter (fences/headings/bullets)
    md = "# Heading\n\n```\nThe quick brown fox jumps\n```\n- over the lazy sleeping dog\n"
    assert content_recall(md, must) == 1.0

    # markdown emphasis / literal quotes glued to words must NOT defeat matching
    # (regression: FastAPI doc emits **OpenAPI**, "schema",  anchors)
    glued = 'generates a **"schema"** with all your *API* using the **OpenAPI** standard'
    assert content_recall(
        glued, ["generates a schema with all your API using the OpenAPI standard"]
    ) == 1.0

    # raw dump: keeps content AND leaks boilerplate -> recall 1 but rejection < 1 -> f1 < 1
    raw_dump = clean + " Subscribe to our newsletter. Edit on GitHub. Accept all cookies."
    assert content_recall(raw_dump, must) == 1.0
    assert boilerplate_rejection(raw_dump, boiler) == 0.0
    assert quality_f1(raw_dump, must, boiler) == 0.0  # rejection 0 -> f1 0

    # partial boilerplate leak -> rejection between 0 and 1
    half_leak = clean + " Subscribe to our newsletter."
    assert abs(boilerplate_rejection(half_leak, boiler) - (2 / 3)) < 1e-9

    # missing content -> recall < 1
    missing = "The quick brown fox jumps over a different ending entirely."
    assert content_recall(missing, must) == 0.5  # 1 of 2 snippets present

    # empty gold lists -> 1.0 by convention
    assert content_recall("anything", []) == 1.0
    assert boilerplate_rejection("anything", []) == 1.0
    assert quality_f1("anything", [], []) == 1.0

    print("scorer self-check OK")
