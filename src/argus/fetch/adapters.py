"""Official-API adapters for hosts that block our IP but publish an open API.

StackExchange question pages 403 our server, while api.stackexchange.com answers
(anonymous quota: 300 req/day/IP). npmjs.com package pages block the VPS directly and
through WARP (reachability map, 2026-10-04), while registry.npmjs.org serves the same
readme as JSON. Requests go through ``fetch_static`` with the
injected client, so every hop keeps the SSRF guard and the size cap; the one POST (the
YouTube player) applies the same guard, no-redirect and size-cap rules itself.
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

from lxml import etree

from .static import _SAFE_ENCODINGS, _check_size, _guard, _stream_capped, fetch_static

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


async def _api_json(url: str, *, client, timeout: float):
    """GET a known JSON endpoint through the guarded hop; a non-200 answer raises."""
    res = await fetch_static(url, client=client, timeout=timeout, api=True)
    if res["status"] != 200:
        raise ValueError(f"status {res['status']}")
    return json.loads(res["html"])


# HN item pages lay the thread out as a table whose nesting the extractor loses; Algolia's
# item API has the story and its whole comment tree in one call (measured 2026-10-31). The
# rendered thread is a nested list: blockquotes flatten in extraction, lists keep indent.
_HN_ITEM = re.compile(r"(?:^|&)id=(\d{1,12})(?:&|$)")
_HN_API = "https://hn.algolia.com/api/v1/items/"
_HN_MAX_DEPTH = 8
_HN_MAX_COMMENTS = 500
# HN text is HTML in which user `<` is already `&lt;`, so a raw `<` opens a tag. Only HN's
# own markup survives (links to http(s) only, attributes dropped); any other tag and a stray
# `<` or `>` are escaped into visible text. HN writes `/` in hrefs as `&#x2F;`.
_HN_MARKUP = re.compile(r"<[^<>]*>|[<>]")
_HN_INLINE = frozenset({"<i>", "</i>", "<b>", "</b>", "<em>", "</em>", "<code>", "</code>",
                        "<pre>", "</pre>", "</a>"})
_HN_LINK = re.compile(r'<a href="(https?:(?://|&#x2F;&#x2F;)[^"<>\s]*)"(?: rel="nofollow")?>')


def hn_target(url: str) -> str | None:
    """The Algolia item API URL for a news.ycombinator.com item page, else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname != "news.ycombinator.com":
        return None
    m = _HN_ITEM.search(parts.query) if parts.path == "/item" else None
    return f"{_HN_API}{m.group(1)}" if m else None


def _hn_markup(m: re.Match) -> str:
    tag = m.group()
    if tag in _HN_INLINE:
        return tag
    if tag == "<p>":
        return "<br>"  # a <p> inside a list item runs into the line before it
    if link := _HN_LINK.fullmatch(tag):
        return f'<a href="{link.group(1)}">'
    return "" if tag == "</p>" else html.escape(tag)


def _hn_count(nodes: list) -> int:
    """Every comment in the tree; iterative, the tree depth is the API's to choose."""
    n, stack = 0, list(nodes)
    while stack:
        n += 1
        stack += stack.pop().get("children") or []
    return n


async def fetch_hn(url: str, *, client, timeout: float = 30) -> dict | None:
    """Story + threaded comments from Algolia as ``{final_url, status, html, render_path:
    'api', structured}``; ``structured`` counts the comments shown against the total.
    Never raises: no match or any failure -> None."""
    api = hn_target(url)
    if api is None:
        return None
    e = html.escape
    shown = 0

    def thread(children: list, depth: int) -> str:
        nonlocal shown
        items = []
        for c in children:
            if shown >= _HN_MAX_COMMENTS:
                break
            shown += 1
            body = _HN_MARKUP.sub(_hn_markup, c.get("text") or "")
            sub = thread(c.get("children") or [], depth + 1) if depth < _HN_MAX_DEPTH else ""
            items.append(f"<li><b>{e(c.get('author') or '[deleted]')}</b> "
                         f"{e(str(c.get('created_at') or '')[:10])}: {body}{sub}</li>")
        return f"<ul>{''.join(items)}</ul>" if items else ""

    try:
        item = await _api_json(api, client=client, timeout=timeout)
        children = item.get("children") or []
        title = e(item.get("title") or f"Hacker News item {item['id']}")
        link = str(item.get("url") or "")
        points = f"{item['points']} points " if item.get("points") is not None else ""
        meta = (f"{e(str(points))}by {e(item.get('author') or '[deleted]')} "
                f"on {e(str(item.get('created_at') or '')[:10])}")
        parts = [f"<html><head><title>{title}</title></head><body><article>",
                 f"<h1>{title}</h1>", f"<p>{meta}</p>"]
        if urlsplit(link).scheme in ("http", "https"):
            parts.append(f'<p><a href="{e(link)}">{e(link)}</a></p>')
        # <p>, not <div>: the extractor duplicates a div of <br>-split inline text.
        parts += [f"<p>{_HN_MARKUP.sub(_hn_markup, item.get('text') or '')}</p>",
                  "<h2>Comments</h2>", thread(children, 1)]
        total = _hn_count(children)
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None
    if total > shown:  # no silent truncation: the cap is stated in the page and the record
        parts.append(f"<p>{total - shown} more comments not shown (limits: depth "
                     f"{_HN_MAX_DEPTH}, {_HN_MAX_COMMENTS} comments).</p>")
    parts.append("</article></body></html>")
    return {"final_url": url, "status": 200, "html": "".join(parts), "render_path": "api",
            "structured": {"id": item["id"], "comments_total": total, "comments_shown": shown}}


# Polymarket event pages are a JS app; the Gamma API serves the event with its markets
# keyless (measured 2026-10-31). outcomes/outcomePrices are JSON encoded as strings; a
# market whose pair does not parse is left out rather than failing the whole event.
_PM_EVENT = re.compile(r"/event/([a-z0-9-]{1,200})(?:/[a-z0-9-]{1,200})?/?")
_PM_API = "https://gamma-api.polymarket.com/events"


def polymarket_target(url: str) -> str | None:
    """The event slug for a polymarket.com event (or event/market) URL, else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "").removeprefix("www.") != "polymarket.com":
        return None
    m = _PM_EVENT.fullmatch(parts.path)
    return m.group(1) if m else None


def _num(value) -> float | None:
    """Gamma sends numbers as numbers or strings; anything else (or NaN/inf) is None."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _text_paras(text) -> str:
    """Plain text as escaped paragraphs, one per non-empty line."""
    return "".join(f"<p>{html.escape(line)}</p>"
                   for line in str(text or "").splitlines() if line.strip())


def _usd(n: float | None) -> str:
    return "n/a" if n is None else f"${n:,.0f}"


async def fetch_polymarket(url: str, *, client, timeout: float = 30) -> dict | None:
    """Event + per-market outcome prices from Gamma as ``{final_url, status, html,
    render_path: 'api', structured}``; ``structured`` carries the prices as 0..1 floats
    for trading consumers. Never raises: no match, no event or any failure -> None."""
    slug = polymarket_target(url)
    if slug is None:
        return None
    e = html.escape
    try:
        event = (await _api_json(f"{_PM_API}?slug={slug}", client=client, timeout=timeout))[0]
        record = {"slug": slug, "title": str(event["title"]), "end_date": event.get("endDate"),
                  "volume": _num(event.get("volume")),
                  "liquidity": _num(event.get("liquidity")), "markets": []}
        title = e(record["title"])
        desc = _text_paras(event.get("description"))
        parts = [f"<html><head><title>{title}</title></head><body><article>",
                 f"<h1>{title}</h1>",
                 f"<p>Ends {e(str(record['end_date'] or 'n/a'))}. Volume "
                 f"{_usd(record['volume'])}, liquidity {_usd(record['liquidity'])}.</p>",
                 desc, "<h2>Markets</h2>"]
        for mk in event.get("markets") or []:
            try:
                outcomes, prices = (v if isinstance(v, list) else json.loads(v)
                                    for v in (mk["outcomes"], mk["outcomePrices"]))
                odds = [{"name": str(o), "price": float(p)}
                        for o, p in zip(outcomes, prices, strict=True)]
            except (KeyError, TypeError, ValueError):  # malformed market: skip, do not raise
                continue
            market = {"question": str(mk.get("question") or ""), "active": mk.get("active"),
                      "closed": mk.get("closed"), "volume": _num(mk.get("volume")),
                      "outcomes": odds}
            record["markets"].append(market)
            shown = ", ".join(f"{e(o['name'])} {o['price'] * 100:.1f}%" for o in odds)
            parts += [f"<h3>{e(market['question'])}</h3>",
                      f"<p>{shown}. Volume {_usd(market['volume'])}, "
                      f"{'closed' if market['closed'] else 'active'}.</p>"]
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None
    parts.append("</article></body></html>")
    return {"final_url": url, "status": 200, "html": "".join(parts), "render_path": "api",
            "structured": record}


# YouTube watch pages carry no transcript; the player API (as the Android app) lists the
# caption tracks and timedtext serves them. Measured from the server 2026-10-31: direct 5/5
# videos, through WARP 0/5 (timedtext 429), so youtube.com stays off the egress proxy list.
# A player that refuses (non-200, LOGIN_REQUIRED, UNPLAYABLE, no videoDetails) -> None, and
# the normal fetch still serves the watch page.
_YT_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YT_PATH = re.compile(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?")
_YT_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_YT_PLAYER = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
# Drifts like X queryIds: when the player starts answering 400 for every video, bump this
# to a current Android app version.
_YT_CLIENT_VERSION = "20.10.38"
_YT_HEADERS = {"user-agent": f"com.google.android.youtube/{_YT_CLIENT_VERSION} "
                             "(Linux; U; Android 11) gzip",
               "accept-encoding": "gzip, deflate"}
_YT_REFUSED = frozenset({"LOGIN_REQUIRED", "UNPLAYABLE"})
_YT_MAX_CUES = 20_000
_YT_PARAGRAPH_S = 30
# Caption XML is the API's, not ours: no entity expansion, DTD or network lookups.
_YT_XML = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)


def youtube_target(url: str) -> str | None:
    """The video id for a YouTube watch/shorts/live/embed or youtu.be URL, else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname or ""
    if host == "youtu.be":
        vid = parts.path[1:].removesuffix("/")
    elif host not in _YT_HOSTS:
        return None
    elif parts.path == "/watch":
        vid = parse_qs(parts.query).get("v", [""])[0]
    else:
        m = _YT_PATH.fullmatch(parts.path)
        vid = m.group(1) if m else ""
    return vid if _YT_ID.fullmatch(vid) else None


def _yt_ts(seconds: float) -> str:
    """``mm:ss``, or ``h:mm:ss`` from one hour on."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _yt_cues(xml: str) -> list[tuple[float, str]]:
    """(start s, text) per cue, from timedtext format 1 (``<text start>``, seconds) or srv3
    (``<p t>``, ms). Cue text is HTML-escaped once more inside the XML."""
    cues = []
    for el in etree.fromstring(xml.encode(), _YT_XML).iter("text", "p"):
        start = float(el.get("start")) if el.tag == "text" else int(el.get("t")) / 1000
        if text := " ".join(html.unescape("".join(el.itertext())).split()):
            cues.append((start, text))
    return cues


def _yt_track(tracks: list[dict]) -> dict:
    """Manual English, any manual, English auto (asr), then the first track."""
    def english(ts: list[dict]) -> dict | None:
        return next((t for t in ts if str(t.get("languageCode")).split("-")[0] == "en"), None)
    manual = [t for t in tracks if t.get("kind") != "asr"]
    return english(manual) or (manual[0] if manual else None) or english(tracks) or tracks[0]


async def _yt_player(vid: str, *, client, timeout: float) -> dict:
    """The Android player answer for ``vid``. fetch_static is GET-only, so this POST keeps
    its rules by hand: guard, no redirects, size-capped streamed body; non-200 raises."""
    await _guard(_YT_PLAYER)
    body = {"context": {"client": {"clientName": "ANDROID", "clientVersion": _YT_CLIENT_VERSION,
                                   "androidSdkVersion": 30, "hl": "en"}}, "videoId": vid}
    req = client.build_request("POST", _YT_PLAYER, json=body, headers=_YT_HEADERS,
                               timeout=timeout)
    resp = await client.send(req, stream=True, follow_redirects=False)
    try:
        if resp.status_code != 200:
            raise ValueError(f"status {resp.status_code}")
        _check_size(resp)
        if resp.headers.get("content-encoding", "").lower().strip() not in _SAFE_ENCODINGS:
            raise ValueError("unsupported content-encoding")
        return json.loads(await _stream_capped(resp))
    finally:
        await resp.aclose()


async def fetch_youtube(url: str, *, client, timeout: float = 30) -> dict | None:
    """Video details + transcript as ``{final_url, status, html, render_path: 'api',
    structured}``. A video without captions still returns its record ("No transcript
    available"). Never raises: no match, a refusing player or any failure -> None."""
    vid = youtube_target(url)
    if vid is None:
        return None
    e = html.escape
    try:
        async with asyncio.timeout(timeout):  # both calls share one budget
            player = await _yt_player(vid, client=client, timeout=timeout)
            status = (player.get("playabilityStatus") or {}).get("status")
            details = player.get("videoDetails")
            if status in _YT_REFUSED or not details:
                return None
            captions = (player.get("captions") or {}).get("playerCaptionsTracklistRenderer")
            tracks = (captions or {}).get("captionTracks") or [] if status == "OK" else []
            track = _yt_track(tracks) if tracks else None
            cues = []
            if track is not None:
                base = urlsplit(track["baseUrl"])  # API data: must stay on YouTube
                if base.scheme != "https" or base.hostname not in _YT_HOSTS:
                    raise ValueError("caption track off youtube.com")
                cues = _yt_cues((await fetch_static(
                    track["baseUrl"], client=client, timeout=timeout, api=True))["html"])
        duration = int(details.get("lengthSeconds") or 0) or None
        record = {"video_id": vid, "title": str(details.get("title") or vid),
                  "channel": str(details.get("author") or ""), "duration_s": duration,
                  "language": track.get("languageCode") if track else None,
                  "auto_generated": track.get("kind") == "asr" if track else None,
                  "languages": list(dict.fromkeys(t.get("languageCode") for t in tracks)),
                  "truncated": len(cues) > _YT_MAX_CUES}
        title = e(record["title"])
        parts = [f"<html><head><title>{title}</title></head><body><article>",
                 f"<h1>{title}</h1>",
                 f"<p>Channel: {e(record['channel'])}. Duration "
                 f"{_yt_ts(duration) if duration else 'n/a'}.</p>",
                 _text_paras(details.get("shortDescription")), "<h2>Transcript</h2>"]
        groups: list[tuple[float, list[str]]] = []  # one paragraph per ~30 s of speech
        for start, text in cues[:_YT_MAX_CUES]:
            if not groups or start >= groups[-1][0] + _YT_PARAGRAPH_S:
                groups.append((start, []))
            groups[-1][1].append(text)
        parts += [f"<p>[{_yt_ts(start)}] {e(' '.join(texts))}</p>" for start, texts in groups]
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None
    if not cues:
        parts.append("<p>No transcript available.</p>")
    if record["truncated"]:  # no silent truncation: stated in the page and the record
        parts.append(f"<p>Transcript truncated at {_YT_MAX_CUES} cues "
                     f"({len(cues) - _YT_MAX_CUES} more not shown).</p>")
    parts.append("</article></body></html>")
    return {"final_url": url, "status": 200, "html": "".join(parts), "render_path": "api",
            "structured": record}


# StockTwits symbol pages are a JS app; the streams API answers keyless with the symbol
# and its latest 30 messages (measured from the server 2026-10-31). It allows ~200 req/h
# per IP, so a 429 is not retried: the read takes the normal page path instead. Bodies
# carry HTML entities; they are unescaped to text, then escaped for our page.
_ST_SYMBOL = re.compile(r"/symbol/([A-Za-z0-9][A-Za-z0-9.-]{0,19})/?")
_ST_API = "https://api.stocktwits.com/api/2/streams/symbol/"
_ST_MAX_MESSAGES = 30


def stocktwits_target(url: str) -> str | None:
    """The upper-cased symbol for a stocktwits.com symbol page, else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "").removeprefix("www.") != "stocktwits.com":
        return None
    m = _ST_SYMBOL.fullmatch(parts.path)
    return m.group(1).upper() if m else None


async def fetch_stocktwits(url: str, *, client, timeout: float = 30) -> dict | None:
    """Symbol + latest messages (newest first) with a sentiment tally as ``{final_url,
    status, html, render_path: 'api', structured}``. Never raises: no match, a 429 or any
    failure -> None."""
    sym = stocktwits_target(url)
    if sym is None:
        return None
    e = html.escape
    try:
        data = await _api_json(f"{_ST_API}{sym}.json", client=client, timeout=timeout)
        if (data.get("response") or {}).get("status") != 200:
            return None
        info = data["symbol"]
        newest = sorted(data.get("messages") or [], reverse=True,
                        key=lambda m: str(m.get("created_at") or ""))
        messages = []
        for m in newest[:_ST_MAX_MESSAGES]:
            user = m.get("user") or {}
            basic = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            messages.append({
                "id": m.get("id"), "created_at": m.get("created_at"),
                "user": str(user.get("username") or ""), "official": bool(user.get("official")),
                "sentiment": basic if basic in ("Bullish", "Bearish") else None,
                "likes": (m.get("likes") or {}).get("total") or 0,
                "body": html.unescape(str(m.get("body") or ""))})
        tally = {k: sum(m["sentiment"] == v for m in messages)
                 for k, v in (("bullish", "Bullish"), ("bearish", "Bearish"), ("unlabeled", None))}
        watch = info.get("watchlist_count")
        record = {"symbol": str(info["symbol"]), "title": str(info.get("title") or ""),
                  "exchange": str(info.get("exchange") or ""),
                  "watchlist_count": watch if isinstance(watch, int) else None, **tally,
                  "sentiment_change": _num(info.get("sentiment_change")),
                  "volume_change": _num(info.get("volume_change")), "messages": messages}
        title = e(f"{record['symbol']} ({record['title']})" if record["title"]
                  else record["symbol"])
        changes = "".join(f" {label} change {record[k]:g}." for k, label in
                          (("sentiment_change", "Sentiment"), ("volume_change", "Volume"))
                          if record[k] is not None)
        parts = [f"<html><head><title>{title}</title></head><body><article>",
                 f"<h1>{title}</h1>",
                 f"<p>Exchange {e(record['exchange'] or 'n/a')}. Watchlists "
                 f"{f'{watch:,}' if record['watchlist_count'] is not None else 'n/a'}.</p>",
                 f"<p>Sentiment over the latest {len(messages)} messages: bullish "
                 f"{tally['bullish']}, bearish {tally['bearish']}, unlabeled "
                 f"{tally['unlabeled']}.{changes}</p>", "<h2>Messages</h2>"]
        for m in messages:
            tag = f" {m['sentiment']}" if m["sentiment"] else ""
            parts += [f"<h3>{e(str(m['created_at'] or ''))} @{e(m['user'])}"
                      f"{' (official)' if m['official'] else ''}{tag}, {e(str(m['likes']))} "
                      f"likes</h3>", _text_paras(m["body"])]
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None
    if len(newest) > _ST_MAX_MESSAGES:
        parts.append(f"<p>{len(newest) - _ST_MAX_MESSAGES} more messages not shown.</p>")
    parts.append("</article></body></html>")
    return {"final_url": url, "status": 200, "html": "".join(parts), "render_path": "api",
            "structured": record}
