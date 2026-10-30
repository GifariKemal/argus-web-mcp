"""FxTwitter client: one X post or profile as JSON without an X login.

api.fxtwitter.com (FxEmbed, MIT) serves the same post data X's embeds use. Requests go
through ``fetch_static`` with the injected client, so every hop keeps the SSRF guard and
the size cap. Everything it returns is untrusted third-party text.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from .. import config
from ..fetch.static import fetch_static

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
    return post_from_fx(t) if t else None


async def fx_user(handle: str, *, client, timeout: float = 30) -> dict | None:
    """One profile: ``{handle, name, verified, followers, following, posts, description,
    url}``, or None."""
    u = await _get(handle, "user", client=client, timeout=timeout)
    if not u:
        return None
    return {**_author(u), "following": u.get("following"), "posts": u.get("tweets"),
            "description": u.get("description", ""), "url": u.get("url", "")}
