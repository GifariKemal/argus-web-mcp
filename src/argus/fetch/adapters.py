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


# Telegram public channels: t.me/s/<channel> is a server-rendered preview of the latest ~20
# posts, no login (measured 2026-10-31 from the primary server). Read as a page it runs
# reaction counts, view counts and "VIEW IN TELEGRAM" stubs into the text; the markup has
# one div per post with its link, time and views. Private chats (/c/, invites) have none.
_TG_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"})
_TG_PATH = re.compile(r"/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/(\d{1,12}))?/?")
_TG_RESERVED = frozenset({"joinchat", "addstickers", "addemoji", "addtheme", "addlist",
                          "share", "proxy", "socks", "login", "setlanguage", "confirmphone",
                          "invoice", "boost", "contact"})
_TG_BEFORE = re.compile(r"(?:^|&)before=(\d{1,12})(?:&|$)")
_TG_POST = ('//div[contains(concat(" ", normalize-space(@class), " "), '
            '" tgme_widget_message ")][@data-post]')
_TG_MEDIA = ('.//*[contains(@class, "message_photo") or contains(@class, "message_video") '
             'or contains(@class, "message_document") or contains(@class, "message_voice") '
             'or contains(@class, "message_sticker") or contains(@class, "message_roundvideo")]')


def telegram_target(url: str) -> str | None:
    """The preview URL for a public channel (``t.me/s/<ch>``) or one post (embed), else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in _TG_HOSTS:
        return None
    m = _TG_PATH.fullmatch(parts.path)
    if m is None or m.group(1).lower() in _TG_RESERVED:
        return None
    channel, post = m.groups()
    if post:
        return f"https://t.me/{channel}/{post}?embed=1&mode=tme"
    before = _TG_BEFORE.search(parts.query)
    return f"https://t.me/s/{channel}" + (f"?before={before.group(1)}" if before else "")


def _tg_first(node, xpath: str):
    found = node.xpath(xpath)
    return found[0] if found else None


def _tg_post(node) -> dict | None:
    channel, _, pid = node.get("data-post", "").rpartition("/")
    if not channel or not pid.isdigit():
        return None
    text_el = _tg_first(node, './/div[contains(concat(" ", normalize-space(@class), " "), '
                              '" js-message_text ")]')
    text = ""
    if text_el is not None:
        for br in text_el.iter("br"):
            br.tail = "\n" + (br.tail or "")
        text = text_el.text_content().strip()
    date = _tg_first(node, './/a[contains(@class, "tgme_widget_message_date")]')
    when = _tg_first(date, ".//time/@datetime") if date is not None else None
    href = date.get("href", "") if date is not None else ""
    views = _tg_first(node, './/span[contains(@class, "tgme_widget_message_views")]')
    return {"id": int(pid),
            "url": href if href.startswith("https://t.me/") else f"https://t.me/{channel}/{pid}",
            "date": str(when or ""), "views": views.text_content().strip() if views is not None
            else "", "text": text, "media": bool(node.xpath(_TG_MEDIA))}


async def fetch_telegram(url: str, *, client, timeout: float = 30) -> dict | None:
    """A public channel's latest posts (or one post), newest first, as ``{final_url, status,
    html, render_path: 'api', structured}``. Never raises: no match, no posts or any
    failure -> None, and the page path reads it as before."""
    page = telegram_target(url)
    if page is None:
        return None
    from lxml import html as lxml_html

    try:
        res = await fetch_static(page, client=client, timeout=timeout)
        if res["status"] != 200:
            return None
        doc = lxml_html.fromstring(res["html"])
        posts = sorted((p for p in map(_tg_post, doc.xpath(_TG_POST)) if p),
                       key=lambda p: p["id"], reverse=True)
        if not posts:
            return None
        channel = _TG_PATH.fullmatch(urlsplit(url).path).group(1)
        # og:title is the bare name; the header also holds the verified badge. A single
        # post (embed) has neither, only the owner name.
        owner = _tg_first(doc, '//*[contains(@class, "tgme_widget_message_owner_name")]')
        title = " ".join(str(_tg_first(doc, '//meta[@property="og:title"]/@content')
                             or (owner.text_content() if owner is not None else "")
                             or channel).split())
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None
    e = html.escape
    parts = [f"<html><head><title>{e(title)}</title></head><body><article>",
             f"<h1>{e(title)}</h1>", f"<p>t.me/{e(channel)}, {len(posts)} posts</p>"]
    for p in posts:
        info = " - ".join(x for x in (p["date"][:16].replace("T", " "),
                                       f"{p['views']} views" if p["views"] else "",
                                       "media" if p["media"] else "") if x)
        body = "<br>".join(e(line) for line in p["text"].split("\n")) or "(media, no text)"
        parts += [f'<h2><a href="{e(p["url"])}">{e(p["url"])}</a></h2>',
                  f"<p>{e(info)}</p>", f"<p>{body}</p>"]
    parts.append("</article></body></html>")
    record = {"channel": channel, "title": title, "posts": posts}
    if len(posts) > 1:  # the preview pages back with ?before=<oldest id>
        record["older_url"] = f"https://t.me/s/{channel}?before={posts[-1]['id']}"
    return {"final_url": url, "status": 200, "html": "".join(parts), "render_path": "api",
            "structured": record}


# GitHub issue and PR pages are a React view the extractor reads as whitespace plus the
# sidebar (astral-sh/uv#1, 2026-11-23) and never as the comment thread. The REST API has the
# body and the comments rendered by GitHub (`body_html`, already sanitised there). The token
# (ARGUS_GITHUB_TOKEN) rides along when set, so redirects are not followed, and since a
# token may see its owner's private repos, the repo is checked public first: a private
# issue must never reach a caller or the shared cache. A 403/429 (rate limit) pauses the
# adapter. Only the conversation URL matches: /files and /commits are a diff, not this. PR
# inline review threads live on another endpoint and are left out; `structured.scope` says so.
_GH_ITEM = re.compile(r"/([\w.-]{1,100})/([\w.-]{1,100})/(issues|pull)/(\d{1,10})/?$")
_GH_PER_PAGE = 100
_GH_MAX_COMMENT_PAGES = 5
_GH_COOLDOWN_S = 300
_gh_backoff_until = 0.0


def github_target(url: str) -> tuple[str, str, str] | None:
    """``(owner, repo, number)`` for a github.com issue or pull request URL, else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "").removeprefix("www.") != "github.com":
        return None
    m = _GH_ITEM.match(parts.path)
    if not m or {m.group(1), m.group(2)} & {".", ".."}:  # dot segments leave /repos/
        return None
    return m.group(1), m.group(2), m.group(4)


async def _gh_json(url: str, *, client, timeout: float, headers: dict):
    res = await fetch_static(url, client=client, timeout=timeout, headers=headers,
                             max_redirects=0, api=True)
    if res["status"] != 200:
        raise ValueError(f"status {res['status']}")
    return json.loads(res["html"])


async def fetch_github(url: str, *, client, timeout: float = 30) -> dict | None:
    """Issue or PR body plus its comments from the REST API as ``{final_url, status, html,
    render_path: 'api', structured}``. Never raises: no match, a private repo, a cool-down
    or any failure -> None. Comment pages that do not fit the time budget are left out and
    the page says how many."""
    global _gh_backoff_until
    target = github_target(url)
    if target is None or time.monotonic() < _gh_backoff_until:
        return None
    from ..gh_search import GH_API, _headers

    owner, repo, number = target
    api = f"{GH_API}/repos/{owner}/{repo}/issues/{number}"
    hdr = {**_headers(), "Accept": "application/vnd.github.html+json"}
    e = html.escape
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    def left() -> float:
        return deadline - loop.time()

    try:
        async with asyncio.timeout(timeout):
            meta = await _gh_json(f"{GH_API}/repos/{owner}/{repo}", client=client,
                                  timeout=timeout, headers=hdr)
            if meta.get("private") is not False:
                return None
            item = await _gh_json(api, client=client, timeout=timeout, headers=hdr)
        total = int(item.get("comments") or 0)
        comments: list = []
        for page in range(1, _GH_MAX_COMMENT_PAGES + 1):
            if len(comments) >= total or left() <= 0:
                break
            try:
                async with asyncio.timeout(left()):
                    batch = await _gh_json(
                        f"{api}/comments?per_page={_GH_PER_PAGE}&page={page}",
                        client=client, timeout=timeout, headers=hdr)
            except TimeoutError:
                break  # keep the pages already read; the shortfall is stated below
            comments += batch
            if len(batch) < _GH_PER_PAGE:
                break
        kind = "pull_request" if item.get("pull_request") else "issue"
        title = e(f"{item['title']} #{item['number']}")
        labels = ", ".join(e(str(lb.get("name", ""))) for lb in item.get("labels") or [])
        meta_line = (f"{'Pull request' if kind == 'pull_request' else 'Issue'} "
                     f"{e(owner)}/{e(repo)}#{e(number)}, {e(str(item.get('state') or ''))}, "
                     f"opened by {e((item.get('user') or {}).get('login') or '[ghost]')} on "
                     f"{e(str(item.get('created_at') or '')[:10])}")
        parts = [f"<html><head><title>{title}</title></head><body><article>",
                 f"<h1>{title}</h1>", f"<p>{meta_line}</p>"]
        if labels:
            parts.append(f"<p>Labels: {labels}</p>")
        parts += [item.get("body_html") or "", f"<h2>Comments ({total})</h2>"]
        for c in comments:
            who = e((c.get("user") or {}).get("login") or "[ghost]")
            parts += [f"<h3>{who} {e(str(c.get('created_at') or '')[:10])}</h3>",
                      c.get("body_html") or ""]
    except Exception as exc:  # noqa: BLE001 - any failure must degrade to None, never raise
        if getattr(exc, "status", None) in (403, 429) or str(exc) in ("status 403",
                                                                      "status 429"):
            _gh_backoff_until = time.monotonic() + _GH_COOLDOWN_S
        return None
    if total > len(comments):  # no silent truncation: the cap is stated in page and record
        parts.append(f"<p>{total - len(comments)} more comments not shown (limit: "
                     f"{_GH_MAX_COMMENT_PAGES * _GH_PER_PAGE} comments within the time "
                     "budget).</p>")
    parts.append("</article></body></html>")
    structured = {"repo": f"{owner}/{repo}", "number": int(number), "kind": kind,
                  "state": item.get("state"), "comments_total": total,
                  "comments_shown": len(comments)}
    if kind == "pull_request":
        structured["scope"] = "conversation comments only; inline review comments not included"
    return {"final_url": url, "status": 200, "html": "".join(parts), "render_path": "api",
            "structured": structured}
