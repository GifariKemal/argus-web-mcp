"""Tests for argus.semantic - offline by default (fake embedder), one slow real-model test.

Most tests monkeypatch the module-level ``_get_embedder`` so the cosine/rerank logic is
exercised deterministically without downloading the ~130MB bge model. The single
``@pytest.mark.slow`` test performs a real embed to confirm the bundled model works.
"""

import builtins

import pytest

from argus import semantic

# Deterministic fake vectors keyed by known words. Orthogonal unit vectors so cosine is exact.
_FAKE = {
    "python": [1.0, 0.0, 0.0],
    "snake": [1.0, 0.0, 0.0],  # identical to python -> cosine 1
    "banana": [0.0, 1.0, 0.0],  # orthogonal to python -> cosine 0
    "half": [0.6, 0.8, 0.0],  # cosine 0.6 vs python
    "zero": [0.0, 0.0, 0.0],  # zero vector -> cosine 0
}


class _FakeEmbedder:
    """Stands in for fastembed.TextEmbedding: .embed(texts) yields fixed vectors."""

    def embed(self, texts):
        for t in texts:
            yield list(_FAKE[t])


@pytest.fixture
def fake_embedder(monkeypatch):
    monkeypatch.setattr(semantic, "_get_embedder", lambda: _FakeEmbedder())
    return _FakeEmbedder()


# --- cosine -----------------------------------------------------------------
def test_cosine_identical_is_one():
    assert semantic.cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert semantic.cosine([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero():
    assert semantic.cosine([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
    assert semantic.cosine([1.0, 0.0, 0.0], [0.0, 0.0, 0.0]) == 0.0


def test_cosine_partial():
    assert semantic.cosine([0.6, 0.8, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(0.6)


# --- embed ------------------------------------------------------------------
def test_embed_empty_returns_empty():
    # Empty input must short-circuit WITHOUT needing an embedder (no download).
    assert semantic.embed([]) == []


def test_embed_returns_list_of_lists(fake_embedder):
    out = semantic.embed(["python", "banana"])
    assert out == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert all(isinstance(v, list) for v in out)


# --- similarities -----------------------------------------------------------
def test_similarities_empty_docs():
    assert semantic.similarities("python", []) == []


def test_similarities_aligned_to_docs_order(fake_embedder):
    sims = semantic.similarities("python", ["snake", "banana", "half"])
    assert sims == pytest.approx([1.0, 0.0, 0.6])


# --- rank_indices -----------------------------------------------------------
def test_rank_indices_orders_by_similarity(fake_embedder):
    # docs: snake(1.0) banana(0.0) half(0.6) -> order 0,2,1
    assert semantic.rank_indices("python", ["snake", "banana", "half"]) == [0, 2, 1]


def test_rank_indices_top_k(fake_embedder):
    assert semantic.rank_indices("python", ["snake", "banana", "half"], top_k=2) == [0, 2]


def test_rank_indices_stable_on_ties(fake_embedder):
    # snake and python both == python (1.0); banana 0.0. Ties keep original order 0,1.
    assert semantic.rank_indices("python", ["snake", "python", "banana"]) == [0, 1, 2]


def test_rank_indices_empty_docs():
    assert semantic.rank_indices("python", []) == []


# --- available --------------------------------------------------------------
def test_available_true_when_installed():
    assert semantic.available() is True


def test_available_false_when_import_fails(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("simulated missing fastembed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert semantic.available() is False


def test_embed_raises_when_unavailable(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("simulated missing fastembed")
        return real_import(name, *args, **kwargs)

    # Reset the cached singleton so _get_embedder actually re-imports.
    monkeypatch.setattr(semantic, "_EMBEDDER", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(semantic.SemanticUnavailable):
        semantic.embed(["python"])


# --- singleton lazy-init (double-checked locking) ---------------------------
def test_get_embedder_uses_lock():
    import threading

    assert isinstance(semantic._EMBEDDER_LOCK, type(threading.Lock()))


def test_get_embedder_constructs_once(monkeypatch):
    constructions = {"n": 0}

    class _Counting:
        def __init__(self, *a, **k):
            constructions["n"] += 1

    import sys
    import types

    fake_mod = types.ModuleType("fastembed")
    fake_mod.TextEmbedding = _Counting
    monkeypatch.setitem(sys.modules, "fastembed", fake_mod)
    monkeypatch.setattr(semantic, "_EMBEDDER", None)

    first = semantic._get_embedder()
    second = semantic._get_embedder()
    assert first is second
    assert constructions["n"] == 1


# --- split_sentences --------------------------------------------------------
def test_split_sentences_multi_sentence_paragraph():
    text = "The cat sat down quietly. A dog ran across the yard! Did the bird fly away?"
    assert semantic.split_sentences(text) == [
        "The cat sat down quietly.",
        "A dog ran across the yard!",
        "Did the bird fly away?",
    ]


def test_split_sentences_drops_short_fragments():
    # "Yes." (1 word) and "No way." (2 words) are < 4 words -> dropped.
    text = "Yes. This sentence has enough words. No way."
    assert semantic.split_sentences(text) == ["This sentence has enough words."]


def test_split_sentences_handles_newlines():
    text = "First line has four words\n\nSecond line also has words"
    assert semantic.split_sentences(text) == [
        "First line has four words",
        "Second line also has words",
    ]


def test_split_sentences_empty():
    assert semantic.split_sentences("") == []
    assert semantic.split_sentences("   \n  ") == []


# --- top_sentences ----------------------------------------------------------
# Five distinct >=4-word sentences used across the top_sentences tests.
_FIVE = (
    "Alpha sentence number one here. "
    "Beta sentence number two here. "
    "Gamma sentence number three here. "
    "Delta sentence number four here. "
    "Epsilon sentence number five here."
)
_FIVE_SPLIT = [
    "Alpha sentence number one here.",
    "Beta sentence number two here.",
    "Gamma sentence number three here.",
    "Delta sentence number four here.",
    "Epsilon sentence number five here.",
]


def _stub_similarities(monkeypatch, scores_by_call):
    """Replace semantic.similarities with a spy returning preset scores; records doc lists."""
    calls = []

    def fake(query, docs):
        calls.append(list(docs))
        return scores_by_call(docs)

    monkeypatch.setattr(semantic, "similarities", fake)
    return calls


def test_top_sentences_returns_top_k_in_descending_order(monkeypatch):
    # Known cosine per sentence index: pick #2 (0.9) > #4 (0.8) > #0 (0.7) as the top 3.
    scores = {
        "Alpha sentence number one here.": 0.7,
        "Beta sentence number two here.": 0.1,
        "Gamma sentence number three here.": 0.9,
        "Delta sentence number four here.": 0.2,
        "Epsilon sentence number five here.": 0.8,
    }
    _stub_similarities(monkeypatch, lambda docs: [scores[d] for d in docs])
    assert semantic.top_sentences("q", _FIVE, top_k=3) == [
        "Gamma sentence number three here.",
        "Epsilon sentence number five here.",
        "Alpha sentence number one here.",
    ]


def test_top_sentences_fewer_than_top_k_returns_all_ranked(monkeypatch):
    text = "First good sentence right here. Second good sentence right here."
    scores = {
        "First good sentence right here.": 0.3,
        "Second good sentence right here.": 0.8,
    }
    _stub_similarities(monkeypatch, lambda docs: [scores[d] for d in docs])
    assert semantic.top_sentences("q", text, top_k=5) == [
        "Second good sentence right here.",
        "First good sentence right here.",
    ]


def test_top_sentences_empty_text_returns_empty(monkeypatch):
    # Must short-circuit before touching similarities at all.
    def boom(query, docs):  # pragma: no cover - asserts it is never called
        raise AssertionError("similarities should not be called for empty text")

    monkeypatch.setattr(semantic, "similarities", boom)
    assert semantic.top_sentences("q", "") == []
    assert semantic.top_sentences("q", "Hi. No.") == []  # only sub-4-word fragments


def test_top_sentences_verbatim(monkeypatch):
    # Returned strings must be the exact sentence text, not normalized/re-joined.
    _stub_similarities(monkeypatch, lambda docs: list(range(len(docs))))
    out = semantic.top_sentences("q", _FIVE, top_k=1)
    assert out == [_FIVE_SPLIT[-1]]  # highest score is last index
    assert out[0] in _FIVE


def test_top_sentences_caps_candidates_and_logs(monkeypatch, caplog):
    # 250 valid sentences -> only first 200 embedded; assert via the spy + truncation log.
    n = 250
    text = " ".join(f"Sentence index number {i} here." for i in range(n))
    calls = _stub_similarities(monkeypatch, lambda docs: [float(i) for i in range(len(docs))])

    with caplog.at_level("WARNING", logger="argus.semantic"):
        out = semantic.top_sentences("q", text, top_k=3)

    assert len(calls) == 1
    assert len(calls[0]) == semantic._MAX_CANDIDATES == 200  # only first 200 embedded
    assert len(out) == 3
    assert any("truncat" in r.message.lower() for r in caplog.records)


# --- real model (slow, downloads ~130MB once) -------------------------------
@pytest.mark.slow
def test_real_embed_semantic_ranking():
    sims = semantic.similarities(
        "python web scraping",
        ["web crawling with python", "banana bread recipe"],
    )
    assert sims[0] > sims[1]
