"""x_search without an account: web-index search for X post URLs, enriched via FxTwitter.

Search engines index a slice of X, mostly posts with some reach; this finds those by
keyword and FxTwitter fills in the full post. Not real-time: ``mode="latest"`` only sorts
what the index has, and says so (``degraded``). X search operators (``from:``, ``lang:``)
mean nothing to web engines, so the query is plain keywords here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time

from ..models import record_stage
from .fxtwitter import FX_TIMEOUT_S, fx_post, x_target

_CONCURRENCY = 4  # FxTwitter is a free public service; stay polite
ENRICH_BUDGET_S = 30  # wall clock for all FxTwitter calls of one search
_TWEPOCH_MS = 1288834974657  # X snowflake epoch (2010-11-04)
_LEGACY_MAX_ID = 30_000_000_000  # pre-snowflake sequential ids carry no timestamp


def _snowflake_ms(post_id: str) -> int | None:
    """Creation time (Unix ms) encoded in an X post id, or None for pre-2010 ids."""
    n = int(post_id)
    return None if n < _LEGACY_MAX_ID else (n >> 22) + _TWEPOCH_MS


def _window_ms(since: date | None, until: date | None) -> tuple[int | None, int | None]:
    lo = int(datetime.combine(since, time.min, UTC).timestamp() * 1000) if since else None
    hi = int(datetime.combine(until, time.max, UTC).timestamp() * 1000) if until else None
    return lo, hi


async def _enrich(ids: list[str], limit: int, client) -> tuple[list[dict], int, bool]:
    """Enrich ids in rank order until ``limit`` posts or the candidates run out, topping
    up for posts FxTwitter no longer has. ``(posts, attempted, timed_out)``."""
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(pid: str) -> dict | None:
        async with sem:
            try:
                return await fx_post(pid, client=client, timeout=FX_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - one bad post must not sink the search
                return None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + ENRICH_BUDGET_S
    posts: list[dict] = []
    attempted = 0
    queue = list(ids)
    while queue and len(posts) < limit:
        batch, queue = queue[:limit - len(posts)], queue[limit - len(posts):]
        attempted += len(batch)
        tasks = [asyncio.create_task(one(pid)) for pid in batch]
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - loop.time()))
        for t in pending:
            t.cancel()
        posts += [r for t in tasks if t in done and (r := t.result())]
        if pending:
            return posts, attempted, True
    return posts, attempted, False


async def search_posts(query: str, *, mode: str, limit: int, since: date | None,
                       until: date | None, search_fn, client) -> dict:
    """``{results, source: "index", degraded, degraded_reason}``. ``search_fn`` is the
    SearXNG wrapper; its SearchError propagates to the tool boundary. Transient reasons
    (anything but ``index_only``) tell the caller not to cache."""
    record_stage("x.index")
    found = await search_fn(query, count=min(limit * 3, 60),
                            include_domains=["x.com", "twitter.com"])
    ids: list[str] = []
    for r in found.get("results", []):
        t = x_target(r.get("url", ""))
        if t and t[0] == "status" and t[1] not in ids:
            ids.append(t[1])

    lo, hi = _window_ms(since, until)
    if lo is not None or hi is not None:  # drop out-of-window ids before paying for them
        ids = [i for i in ids if (ms := _snowflake_ms(i)) is None
               or ((lo is None or ms >= lo) and (hi is None or ms <= hi))]
    latest = mode == "latest"
    if latest:  # newest candidates first, so the limit keeps the recent ones
        ids.sort(key=lambda i: _snowflake_ms(i) or 0, reverse=True)

    posts, attempted, timed_out = await _enrich(ids, limit, client)
    enriched = len(posts)
    for _ in posts:
        record_stage("x.fxtwitter")
    lo_s = f"{since.isoformat()}T00:00:00Z" if since else None
    hi_s = f"{until.isoformat()}T23:59:59Z" if until else None
    posts = [p for p in posts if (lo_s is None or (p["created_at"] or "") >= lo_s)
             and (hi_s is None or (p["created_at"] or "") <= hi_s)]
    if latest:  # ISO 8601 UTC strings sort chronologically
        posts.sort(key=lambda p: p["created_at"] or "", reverse=True)

    reason = None
    if found.get("degraded"):
        reason = found.get("degraded_reason") or "search_degraded"
    elif timed_out:
        reason = "enrich_timeout"
    elif attempted and enriched * 2 < attempted:
        reason = "partial_enrichment"
    elif latest:
        reason = "index_only"
    return {"results": posts, "source": "index", "degraded": reason is not None,
            "degraded_reason": reason}
