"""FxTwitter client: one X post or profile as JSON without an X login.

api.fxtwitter.com (FxEmbed, MIT) serves the same post data X's embeds use. Requests go
through ``fetch_static`` with the injected client, so every hop keeps the SSRF guard and
the size cap. Everything it returns is untrusted third-party text.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from .. import config
from ..fetch.static import fetch_static

# Per FxTwitter call: a hung public service must not eat a read's whole budget, which
# would leave no time for the normal-ladder fallback.
FX_TIMEOUT_S = 10

_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"})
_HANDLE = r"[A-Za-z0-9_]{1,15}"
_STATUS = re.compile(rf"/(?:{_HANDLE}|i/web)/status(?:es)?/(\d{{1,20}})(?:/.*)?")
_PROFILE = re.compile(rf"/({_HANDLE})/?")
# First path segments that are X pages, never a user.
_RESERVED = frozenset({
    "home", "explore", "search", "i", "settings", "notifications", "messages", "compose",
    "login", "logout", "signup", "tos", "privacy", "hashtag", "intent", "share", "account",
    "jobs", "premium",
})


def x_target(url: str) -> tuple[Literal["status", "user"], str] | None:
    """``("status", id)`` or ``("user", handle)`` for an X post or profile URL, else None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    if parts.scheme not in ("http", "https") or host not in _HOSTS:
        return None
    if m := _STATUS.fullmatch(parts.path):
        return ("status", m.group(1))
    if (m := _PROFILE.fullmatch(parts.path)) and m.group(1).lower() not in _RESERVED:
        return ("user", m.group(1))
    return None


def _author(a: dict) -> dict:
    return {
        "handle": a.get("screen_name", ""),
        "name": a.get("name", ""),
        "verified": bool((a.get("verification") or {}).get("verified")),
        "followers": a.get("followers"),
    }


def post_from_fx(t: dict) -> dict:
    """FxTwitter ``tweet`` object -> Argus post model."""
    ts = t.get("created_timestamp")
    created = (datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
               if isinstance(ts, int | float) else None)
    quote = t.get("quote")
    return {
        "id": str(t.get("id", "")),
        "url": t.get("url", ""),
        "created_at": created,
        "text": t.get("text", ""),
        "lang": t.get("lang"),
        "author": _author(t.get("author") or {}),
        "metrics": {"replies": t.get("replies"), "reposts": t.get("retweets"),
                    "likes": t.get("likes"), "quotes": t.get("quotes"),
                    "views": t.get("views"), "bookmarks": t.get("bookmarks")},
        "media": [{"type": m.get("type"), "url": m.get("url")}
                  for m in (t.get("media") or {}).get("all") or []],
        "quoted": post_from_fx(quote) if isinstance(quote, dict) else None,
        "in_reply_to": t.get("replying_to_status"),
        "source": "fxtwitter",
    }


async def _get(path: str, key: str, *, client, timeout: float) -> dict | None:
    """The ``key`` object of an FxTwitter answer, or None. Never raises: FxTwitter answers
    200 with an HTML page for deleted or unknown posts, so only code 200 JSON counts."""
    try:
        res = await fetch_static(f"{config.X_FXTWITTER_URL}/{path}", client=client,
                                 timeout=timeout)
        data = json.loads(res["html"])
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None
    obj = data.get(key) if isinstance(data, dict) and data.get("code") == 200 else None
    return obj if isinstance(obj, dict) else None


async def fx_post(post_id: str, *, client, timeout: float = 30) -> dict | None:
    """One post by id as the Argus post model, or None."""
    t = await _get(f"status/{post_id}", "tweet", client=client, timeout=timeout)
    try:
        return post_from_fx(t) if t else None
    except Exception:  # noqa: BLE001 - an unexpected upstream shape degrades to None
        return None


async def fx_user(handle: str, *, client, timeout: float = 30) -> dict | None:
    """One profile: ``{handle, name, verified, followers, following, posts, description,
    url}``, or None."""
    u = await _get(handle, "user", client=client, timeout=timeout)
    if not u:
        return None
    try:
        return {**_author(u), "following": u.get("following"), "posts": u.get("tweets"),
                "description": u.get("description", ""), "url": u.get("url", "")}
    except Exception:  # noqa: BLE001 - an unexpected upstream shape degrades to None
        return None


def _post_block(p: dict) -> str:
    e = html.escape
    a = p["author"]
    m = p["metrics"]
    text = e(p["text"]).replace("\n", "<br>")
    stats = ", ".join(f"{k} {v}" for k, v in m.items() if v is not None)
    media = "".join(f'<li><a href="{e(x["url"] or "")}">{e(x["type"] or "media")}</a></li>'
                    for x in p["media"])
    quoted = f"<blockquote>{_post_block(p['quoted'])}</blockquote>" if p["quoted"] else ""
    return (f"<p><strong>{e(a['name'])} (@{e(a['handle'])})</strong> {e(p['created_at'] or '')}</p>"
            f"<p>{text}</p><p>{e(stats)}</p>"
            + (f"<ul>{media}</ul>" if media else "") + quoted)


def render_html(post: dict | None = None, user: dict | None = None) -> str:
    """A minimal page for the extractor. Every field is HTML-escaped: post text is
    untrusted and may carry markup."""
    e = html.escape
    if post is not None:
        title = f"@{post['author']['handle']}: {post['text'][:80]}"
        body = _post_block(post)
    else:
        u = user or {}
        title = f"{u.get('name', '')} (@{u.get('handle', '')})"
        body = (f"<h1>{e(title)}</h1><p>{e(u.get('description') or '')}</p>"
                f"<p>followers {e(str(u.get('followers')))}, following "
                f"{e(str(u.get('following')))}, posts {e(str(u.get('posts')))}</p>")
    return (f"<html><head><title>{e(title)}</title></head><body><article>{body}"
            f"</article></body></html>")


async def fetch_x(url: str, *, client, timeout: float = 30) -> dict | None:
    """An X post or profile URL as a fetch result (``render_path: 'api'``, plus
    ``structured`` = post or user model), or None when ``url`` is not one or FxTwitter
    failed (the caller then takes the normal fetch path)."""
    target = x_target(url)
    if target is None:
        return None
    kind, ident = target
    if kind == "status":
        obj = await fx_post(ident, client=client, timeout=timeout)
    else:
        obj = await fx_user(ident, client=client, timeout=timeout)
    try:
        page = None if not obj else (
            render_html(post=obj) if kind == "status" else render_html(user=obj))
    except Exception:  # noqa: BLE001 - odd field types: take the normal fetch path instead
        page = None
    if page is None:
        return None
    return {"final_url": url, "status": 200, "html": page, "render_path": "api",
            "structured": obj}
