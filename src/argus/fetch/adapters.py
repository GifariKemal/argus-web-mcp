"""Official-API adapters for hosts that block our IP but publish an open API.

StackExchange question pages 403 our server, while api.stackexchange.com answers
(anonymous quota: 300 req/day/IP). npmjs.com package pages block the VPS directly and
through WARP (reachability map, 2026-10-04), while registry.npmjs.org serves the same
readme as JSON. Requests go through ``fetch_static`` with the
injected client, so every hop keeps the SSRF guard and the size cap.
"""

from __future__ import annotations

import asyncio
import html
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


_NPM_PACKAGE = re.compile(r"/package/((?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*)(?:/|$)")


def npm_target(url: str) -> str | None:
    """The package name for an npmjs.com package page (scoped names included), else None."""
    parts = urlsplit(url)
    if (parts.hostname or "").removeprefix("www.") != "npmjs.com":
        return None
    m = _NPM_PACKAGE.match(parts.path)
    return m.group(1) if m else None


async def fetch_npm(url: str, *, client, timeout: float = 30) -> dict | None:
    """Package readme + metadata from the registry as ``{final_url, status, html,
    render_path: 'api'}``. Never raises: no match or any failure -> None."""
    name = npm_target(url)
    if name is None:
        return None
    try:
        data = json.loads((await fetch_static(
            f"https://registry.npmjs.org/{name}", client=client, timeout=timeout))["html"])
        latest = (data.get("dist-tags") or {}).get("latest", "")
        meta = f"<p>Latest version: {html.escape(str(latest))}</p>" if latest else ""
        if data.get("homepage"):
            home = html.escape(str(data["homepage"]))
            meta += f'<p>Homepage: <a href="{home}">{home}</a></p>'
        readme = html.escape(str(data.get("readme") or ""))
        desc = html.escape(str(data.get("description") or ""))
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None
    # The readme is markdown; <pre> keeps it readable and verbatim through extraction.
    # Registry text is untrusted markup (readmes embed HTML, code samples carry `<`).
    page = (f"<html><head><title>{name}</title></head><body><article><h1>{name}</h1>"
            f"<p>{desc}</p>{meta}<pre>{readme}</pre></article></body></html>")
    return {"final_url": url, "status": 200, "html": page, "render_path": "api"}


# Reddit thread pages come back as a JS shell whose comments the extractor loses, and its
# JSON API 403s this server, while the thread's Atom feed answers 200 with the post and
# its top comments as rendered HTML (measured 2026-10-30). The feed 429s a burst, so a 429
# (or a 403) pauses the adapter and reads take the normal (browser) path meanwhile. A
# comment permalink reads as its whole thread.
_REDDIT_HOSTS = frozenset({"reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com"})
_REDDIT_THREAD = re.compile(r"/r/([A-Za-z0-9_]{2,21})/comments/([a-z0-9]{1,12})(?:/|$)")
_ATOM = "{http://www.w3.org/2005/Atom}"
_REDDIT_COOLDOWN_S = 60
_reddit_backoff_until = 0.0


def reddit_target(url: str) -> str | None:
    """The thread's Atom feed URL for a reddit post/comments URL, else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "").removeprefix("www.") not in _REDDIT_HOSTS:
        return None
    m = _REDDIT_THREAD.match(parts.path)
    return f"https://www.reddit.com/r/{m.group(1)}/comments/{m.group(2)}/.rss" if m else None


def _reddit_entry(entry) -> tuple[str, str, str, str]:
    """(title, author, date, html body) of one Atom entry; the feed's link/comments
    trailer after ``<!-- SC_ON -->`` is dropped."""
    def text(tag: str) -> str:
        el = entry.find(_ATOM + tag)
        return (el.text or "") if el is not None else ""
    author = entry.find(f"{_ATOM}author/{_ATOM}name")
    body = text("content").split("<!-- SC_ON -->")[0]
    return text("title"), (author.text or "") if author is not None else "", \
        text("updated")[:10], body


async def fetch_reddit(url: str, *, client, timeout: float = 30) -> dict | None:
    """Post + top comments from the thread feed as ``{final_url, status, html,
    render_path: 'api'}``. Never raises: no match, a cool-down or any failure -> None."""
    global _reddit_backoff_until
    feed = reddit_target(url)
    if feed is None or time.monotonic() < _reddit_backoff_until:
        return None
    from xml.etree import ElementTree as ET  # stdlib expat refuses entity expansion bombs

    try:
        xml = (await fetch_static(feed, client=client, timeout=timeout, api=True))["html"]
        entries = ET.fromstring(xml).findall(_ATOM + "entry")  # noqa: S314 - see import
        if not entries:
            return None
        title, author, day, post = _reddit_entry(entries[0])
        e = html.escape
        parts = [f"<html><head><title>{e(title)}</title></head><body><article>",
                 f"<h1>{e(title)}</h1><p>{e(author)} {e(day)}</p>", post,
                 "<h2>Comments</h2>"]
        for c in entries[1:]:
            _, c_author, c_day, c_body = _reddit_entry(c)
            parts += [f"<h3>{e(c_author)} {e(c_day)}</h3>", c_body]
    except Exception as exc:  # noqa: BLE001 - any failure must degrade to None, never raise
        if getattr(exc, "status", None) in (403, 429):
            _reddit_backoff_until = time.monotonic() + _REDDIT_COOLDOWN_S
        return None
    parts.append("</article></body></html>")
    return {"final_url": url, "status": 200, "html": "".join(parts), "render_path": "api"}
