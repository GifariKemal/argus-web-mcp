"""Local semantic embedding + rerank - ONNX multilingual MiniLM via fastembed (no torch).

The model (~470MB, baked into the image) is lazily loaded on first ``embed``; the
``TextEmbedding`` instance is a module singleton reused across calls. ``available()`` only
checks importability, so it never triggers a download. numpy powers the cosine/batch math.
"""

import logging
import re
import threading

import numpy as np

logger = logging.getLogger(__name__)

# Multilingual since 0.4.21: the workload is mostly Indonesian, and the English-only
# bge-small-en scored Indonesian junk 0.64-0.86, above every floor (benchmark/
# semantic_id.py: Indonesian AUC 0.894 -> 0.960, English 1.000 -> 0.956, +400 MB RSS).
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Sentence-ish splitter: break after .!? + whitespace, or on blank/newline runs. Good enough
# for highlight selection - we never need perfect linguistic segmentation here.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Cap how many sentences we embed per call - embedding cost is linear and highlights only ever
# come from the top handful, so the tail is wasted compute.
_MAX_CANDIDATES = 200

# fastembed defaults to batch_size=256. One batch of 256 x 512-token inputs needs GBs of
# attention scratch, and ONNX Runtime's CPU arena keeps that peak for the life of the
# process - the live container sat at 2.7 GB RSS. Measured on 48 x 512-token docs:
# batch 48 kept +1005 MB, 16 kept +516 MB, 8 kept +263 MB, at 9-12 s either way (CPU
# bound), so a small batch only lowers the high-water mark.
_EMBED_BATCH = 8

_EMBEDDER = None  # lazily-created TextEmbedding singleton
_EMBEDDER_LOCK = threading.Lock()  # guards the lazy init against concurrent first-calls


class SemanticUnavailable(Exception):
    """Raised when fastembed cannot be imported."""


def available() -> bool:
    """True if fastembed is importable (no model download triggered - import only)."""
    try:
        __import__("fastembed")
    except ImportError:
        return False
    return True


def _get_embedder():
    """Lazily create and cache the TextEmbedding singleton (double-checked locking)."""
    global _EMBEDDER
    if _EMBEDDER is None:
        with _EMBEDDER_LOCK:
            if _EMBEDDER is None:  # re-check inside the lock - another thread may have won
                try:
                    from fastembed import TextEmbedding
                except ImportError as exc:  # pragma: no cover - via monkeypatched import
                    raise SemanticUnavailable("fastembed is not installed") from exc
                _EMBEDDER = TextEmbedding(model_name=MODEL_NAME)
    return _EMBEDDER


def warm() -> bool:
    """Best-effort: load the embedder + run one tiny embed so the first real query
    doesn't pay the ~5s model download/load. Returns True if warmed, False if the
    [semantic] extra is absent. Never raises - callers (startup) must not be blocked.
    """
    if not available():
        return False
    try:
        embed(["warm"])
    except Exception as exc:  # noqa: BLE001 - warm-up is opportunistic; never fail startup
        logger.warning("semantic warm-up skipped: %s", exc)
        return False
    return True


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with the singleton model. Empty input -> []. Raises SemanticUnavailable."""
    if not texts:
        return []
    vectors = _get_embedder().embed(texts, batch_size=_EMBED_BATCH)
    return [np.asarray(vec, dtype=np.float64).tolist() for vec in vectors]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; zero vector on either side -> 0.0."""
    va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def similarities(query: str, docs: list[str]) -> list[float]:
    """Embed [query]+docs in one batch; return cosine(query, doc) aligned to docs."""
    if not docs:
        return []
    vectors = embed([query, *docs])
    q = vectors[0]
    return [cosine(q, d) for d in vectors[1:]]


def rank_indices(query: str, docs: list[str], top_k: int | None = None) -> list[int]:
    """Doc indices sorted by descending similarity (stable on ties), capped to top_k."""
    sims = similarities(query, docs)
    order = sorted(range(len(sims)), key=lambda i: -sims[i])  # stable sort keeps ties in order
    return order[:top_k] if top_k is not None else order


def split_sentences(text: str) -> list[str]:
    """Split text into trimmed sentence-ish units; drop units < 4 words. Pure, stdlib."""
    return [s for unit in _SENT_SPLIT.split(text) if len((s := unit.strip()).split()) >= 4]


def top_sentences(query: str, text: str, top_k: int = 3) -> list[str]:
    """Return up to ``top_k`` sentences from ``text`` most semantically similar to ``query``.

    Uses ``similarities(query, sentences)`` and keeps the highest-cosine sentences verbatim,
    in descending-similarity order. Returns ``[]`` when ``text`` has no usable sentences. At
    most ``_MAX_CANDIDATES`` sentences are embedded (the rest are dropped + logged). Raises only
    what ``embed``/``similarities`` raise; callers gate on ``available()``.
    """
    sentences = split_sentences(text)
    if not sentences:
        return []
    if len(sentences) > _MAX_CANDIDATES:
        logger.warning(
            "top_sentences: truncating %d candidate sentences to %d for embedding",
            len(sentences),
            _MAX_CANDIDATES,
        )
        sentences = sentences[:_MAX_CANDIDATES]
    return [sentences[i] for i in rank_indices(query, sentences, top_k=top_k)]
