"""Ranked news feed with optional owned-LLM sentiment scoring.

Ranking reuses the existing SearXNG-backed `search` (news category). Sentiment is
opt-in and degrades gracefully: if the owned LLM is unavailable (module missing or
backend down) items are returned without a ``score`` rather than failing.
"""

from __future__ import annotations

from argus.search import search as web_search

_SENTIMENT_SCHEMA = {"score": "float"}


def _llm_hooks():
    """Return (extract_llm, llm_available) or (None, None) if LLM is unavailable.

    The LLM extractor is optional and may not be installed; importing lazily keeps
    the trading package usable without it.
    """
    try:
        from argus.extract.llm import extract_llm, llm_available
    except ImportError:
        return None, None
    return extract_llm, llm_available


async def _score_item(extract_llm, item: dict) -> float | None:
    text = f"{item.get('title', '')}\n{item.get('snippet', '')}".strip()
    if not text:
        return None
    try:
        result = await extract_llm(
            text,
            _SENTIMENT_SCHEMA,
            prompt=(
                "Rate the market sentiment of this news from -1 (very bearish) "
                "to 1 (very bullish). Respond with the score only."
            ),
        )
    except Exception:  # noqa: BLE001 - LLM failure must not break the feed
        return None
    raw = result.get("score") if isinstance(result, dict) else None
    if raw is None:
        return None
    try:
        return max(-1.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return None


async def news_sentiment_feed(query, since=None, *, sentiment=False, client=None) -> dict:
    """Rank news for ``query`` and optionally attach an owned-LLM sentiment score.

    Returns ``{query, items, count}`` where each item is
    ``{title, url, snippet, published?, score?}``. ``score`` is only present when
    ``sentiment=True`` AND the owned LLM is available; otherwise it is omitted.
    """
    kwargs = {"category": "news"}
    if since:
        kwargs["time_range"] = since
    if client is not None:
        kwargs["client"] = client

    found = await web_search(query, **kwargs)

    items: list[dict] = []
    for r in found.get("results", []):
        item = {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
        }
        if r.get("published"):
            item["published"] = r["published"]
        items.append(item)

    if sentiment:
        extract_llm, llm_available = _llm_hooks()
        if extract_llm is not None and llm_available():
            for item in items:
                score = await _score_item(extract_llm, item)
                if score is not None:
                    item["score"] = score

    # Propagate the search layer's degraded signal (low_relevance / backend_failover) -
    # off-topic or failed-over news must never be served as clean trading input.
    out = {"query": query, "items": items, "count": len(items),
           "degraded": bool(found.get("degraded"))}
    if found.get("degraded_reason"):
        out["degraded_reason"] = found["degraded_reason"]
    return out
