"""Official-API adapters for hosts that block our IP but publish an open API.

StackExchange question pages 403 our server, while api.stackexchange.com answers
(anonymous quota: 300 req/day/IP). Requests go through ``fetch_static`` with the
injected client, so every hop keeps the SSRF guard and the size cap.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlsplit

from .static import fetch_static

_SE_API = "https://api.stackexchange.com/2.3/questions/"
_SE_SITES = {
    "stackoverflow.com": "stackoverflow",
    "superuser.com": "superuser",
    "serverfault.com": "serverfault",
    "askubuntu.com": "askubuntu",
}
_SE_SUBSITE = re.compile(r"([a-z0-9-]+)\.stackexchange\.com")
_SE_QUESTION = re.compile(r"/(?:questions|q)/(\d+)(?:/|$)")


def stackexchange_target(url: str) -> tuple[str, str] | None:
    """``(api_site, question_id)`` for a StackExchange question URL, else None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    site = _SE_SITES.get(host)
    if site is None and (m := _SE_SUBSITE.fullmatch(host)):
        site = m.group(1)
    q = _SE_QUESTION.match(parts.path)
    return (site, q.group(1)) if site and q else None


# The API sets "backoff" (seconds) when it wants callers to pause; ignoring it gets an IP
# banned, and the anonymous quota (300/day) is shared by every caller of this server.
_backoff_until = 0.0


async def _items(url: str, *, client, timeout: float) -> list[dict]:
    global _backoff_until
    data = json.loads((await fetch_static(url, client=client, timeout=timeout))["html"])
    if data.get("backoff"):
        _backoff_until = time.monotonic() + float(data["backoff"])
    return data["items"]


async def fetch_stackexchange(url: str, *, client, timeout: float = 30) -> dict | None:
    """Question + top-voted answers via the API, as ``{final_url, status, html,
    render_path: 'api'}``. Never raises: no match or any failure -> None."""
    target = stackexchange_target(url)
    if target is None:
        return None
    site, qid = target
    if time.monotonic() < _backoff_until:
        return None
    query = f"site={site}&filter=withbody"
    try:
        async with asyncio.timeout(timeout):  # both calls share one budget
            (question,) = await _items(f"{_SE_API}{qid}?{query}", client=client, timeout=timeout)
            answers = await _items(
                f"{_SE_API}{qid}/answers?{query}&sort=votes&order=desc&pagesize=5",
                client=client, timeout=timeout,
            )
            # The API returns title as already-escaped HTML and bodies as rendered HTML.
            title = question["title"]
            parts = [f"<html><head><title>{title}</title></head><body><article>",
                     f"<h1>{title}</h1>", question["body"], "<h2>Answers</h2>"]
            for a in answers:
                mark = ", accepted" if a.get("is_accepted") else ""
                parts += [f"<h3>Answer (score {a['score']}{mark})</h3>", a["body"]]
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None
    parts.append("</article></body></html>")
    return {"final_url": url, "status": 200, "html": "".join(parts), "render_path": "api"}
