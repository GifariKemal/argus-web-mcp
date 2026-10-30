"""x_search without an account: web-index search for X post URLs, enriched via FxTwitter.

Search engines index a slice of X, mostly posts with some reach; this finds those by
keyword and FxTwitter fills in the full post. Not real-time: ``mode="latest"`` only sorts
what the index has, and says so (``degraded``).
"""

from __future__ import annotations

import asyncio
from datetime import date

from ..models import record_stage
from .fxtwitter import fx_post, x_target

_CONCURRENCY = 4  # FxTwitter is a free public service; stay polite


async def search_posts(query: str, *, mode: str, limit: int, since: date | None,
                       until: date | None, search_fn, client) -> dict:
    """``{results, source: "index", degraded, degraded_reason}``. ``search_fn`` is the
    SearXNG wrapper; its SearchError propagates to the tool boundary."""
    record_stage("x.index")
    found = await search_fn(query, count=min(limit * 3, 60),
                            include_domains=["x.com", "twitter.com"])
    ids: list[str] = []
    for r in found.get("results", []):
        t = x_target(r.get("url", ""))
        if t and t[0] == "status" and t[1] not in ids:
            ids.append(t[1])
    ids = ids[:limit]

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def enrich(pid: str) -> dict | None:
        async with sem:
            return await fx_post(pid, client=client)

    posts = [p for p in await asyncio.gather(*(enrich(i) for i in ids)) if p]
    for _ in posts:
        record_stage("x.fxtwitter")
    lo = f"{since.isoformat()}T00:00:00Z" if since else None
    hi = f"{until.isoformat()}T23:59:59Z" if until else None
    posts = [p for p in posts if (lo is None or (p["created_at"] or "") >= lo)
             and (hi is None or (p["created_at"] or "") <= hi)]
    latest = mode == "latest"
    if latest:  # ISO 8601 UTC strings sort chronologically
        posts.sort(key=lambda p: p["created_at"] or "", reverse=True)
    return {"results": posts, "source": "index", "degraded": latest,
            "degraded_reason": "index_only" if latest else None}
