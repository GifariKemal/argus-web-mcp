"""x_search Layer 1: X web GraphQL SearchTimeline with one cookie account (opt-in).

Active only when ARGUS_X_AUTH_TOKEN and ARGUS_X_CT0 are set. The queryId and feature
switches are read live from X's web bundles (idea from Scweet, MIT) with a pinned fallback,
so an X-side rotation heals on the next refresh instead of needing a release. Every call
is paced by ``Limiter`` and goes out from the server IP through the SSRF-guarded client
with ``max_redirects=0``, so the cookies never follow a redirect to another host. Cookie
values never appear in errors, logs or results.

Fallback queryId and features come from twscrape (MIT, (c) 2023 vladkens), commit 63f61a5.
"""

from __future__ import annotations

import asyncio
import html
import json
import random
import re
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode, urljoin

from .. import config
from ..fetch.static import FetchError, fetch_static
from ..models import record_stage
from .xclid import INDICES_FILE_RE, XClIdError, XClIdGen, get_scripts_list

OP = "SearchTimeline"
_FALLBACK_QID = "BYppgupFFS2yqVmmRNjlsw"
# The public bearer every x.com web session sends; a constant of X's web app, not a secret.
_BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
           "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
FEATURES = {
    "articles_preview_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": False,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweet_with_visibility_results_prefer_gql_media_interstitial_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_image_annotation_enabled": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "responsive_web_jetfuel_frame": False,
    "rweb_video_screen_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
}
BOOT_TTL_S = 6 * 3600
_REQ_TIMEOUT_S = 20
_MAX_PAGES = 3  # 20 posts per page; limit is at most 50
_BUNDLE_CONCURRENCY = 8
_MAX_BUNDLES = 300

# X error code -> (Argus code, cooldown seconds). 88 rate limit; 32/89/215/353 bad or
# stale cookies; 64/141/326 locked or suspended account.
_X_CODES = {88: ("x_rate_limited", 900), 32: ("x_account_expired", 0),
            89: ("x_account_expired", 0), 215: ("x_account_expired", 0),
            353: ("x_account_expired", 0), 64: ("x_account_locked", 0),
            141: ("x_account_locked", 0), 326: ("x_account_locked", 0)}
_STATE_OF = {"x_rate_limited": "rate_limited", "x_account_expired": "expired",
             "x_account_locked": "locked", "x_account_rejected": "rejected",
             "x_schema_changed": "schema_changed", "x_backend_error": "error"}
_ACCOUNT_CODES = frozenset({"x_rate_limited", "x_account_expired", "x_account_locked",
                            "x_account_rejected"})
# A failed bootstrap is not retried for this long, so a dead account does not load an
# authenticated page on every call; a forced refresh needs a manifest at least this old.
_BOOT_BACKOFF_S = 900
_MIN_REFRESH_S = 600


class XError(Exception):
    """A Layer 1 failure. ``code`` is an Argus error code; ``retry_after`` in seconds."""

    def __init__(self, code: str, message: str, retry_after: int | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(message)


def enabled() -> bool:
    return bool(config.X_AUTH_TOKEN and config.X_CT0)


class Limiter:
    """Per-account pacing: ``per_window`` calls per ``window_s``, ``per_day`` per rolling
    24 h, a cooldown after X pushes back, and a 1-3 s jittered gap between calls. Over a
    limit it fails fast (``x_rate_limited`` + ``retry_after``) instead of queueing a tool
    call for minutes."""

    def __init__(self, per_window: int, window_s: float, per_day: int,
                 clock=time.monotonic) -> None:
        self.per_window, self.window_s, self.per_day = per_window, window_s, per_day
        self.clock = clock
        self.calls: deque[float] = deque()
        self.cooldown_until = 0.0
        self.last = -1e9
        self.lock = asyncio.Lock()

    def cool(self, seconds: float) -> None:
        self.cooldown_until = max(self.cooldown_until, self.clock() + seconds)

    def wait_needed(self) -> float:
        """Seconds until a call is allowed (0 = now)."""
        now = self.clock()
        while self.calls and now - self.calls[0] >= 86400:
            self.calls.popleft()
        waits = [self.cooldown_until - now]
        if len(self.calls) >= self.per_day:
            waits.append(self.calls[0] + 86400 - now)
        recent = [t for t in self.calls if now - t < self.window_s]
        if len(recent) >= self.per_window:
            waits.append(recent[0] + self.window_s - now)
        return max(0.0, *waits)

    async def acquire(self) -> None:
        async with self.lock:
            wait = self.wait_needed()
            if wait > 0:
                raise XError("x_rate_limited", "X account pacing limit reached",
                             retry_after=int(wait) + 1)
            gap = 1.0 + random.uniform(0, 2) - (self.clock() - self.last)  # noqa: S311 - jitter
            if gap > 0:
                await asyncio.sleep(gap)
            self.last = self.clock()
            self.calls.append(self.last)


_limiter: Limiter | None = None
_boot: dict | None = None  # {"gen", "qid", "features", "at"}
_boot_fail: tuple[float, str] | None = None  # (retry not before, XError code)
_state = "unknown"
_locks: dict = {}


def _boot_lock() -> asyncio.Lock:
    """One bootstrap at a time (per event loop: production runs one, tests many)."""
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks.clear()
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


def _get_limiter() -> Limiter:
    global _limiter
    if _limiter is None:
        _limiter = Limiter(config.X_MAX_PER_WINDOW, 900, config.X_DAILY_REQUESTS)
    return _limiter


def status() -> dict:
    """For /health: ``x_account`` off|unknown|ok|rate_limited|locked|expired|rejected|
    schema_changed|error and the manifest age."""
    if not enabled():
        return {"x_account": "off"}
    age = None if _boot is None else int(time.monotonic() - _boot["at"])
    return {"x_account": _state, "x_manifest_age_s": age}


# X serves its web app (and accepts the session) only for a browser; measured 2026-10-30:
# the page with API headers answers 401, cookie + browser UA answers the logged-in app.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/141.0.0.0 Safari/537.36")


def _page_headers() -> dict:
    """What a browser sends for the x.com page: the session cookie, no API headers."""
    return {"user-agent": _UA, "cookie": f"auth_token={config.X_AUTH_TOKEN}; ct0={config.X_CT0}"}


def _cookie_headers() -> dict:
    return {
        "user-agent": _UA,
        "authorization": _BEARER,
        "cookie": f"auth_token={config.X_AUTH_TOKEN}; ct0={config.X_CT0}",
        "x-csrf-token": config.X_CT0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "referer": "https://x.com/",
    }


def features_for(bundle: str, op: str) -> dict | None:
    """The feature switches the web app sends with ``op``, from one JS bundle, valued from
    FEATURES (unknown new switches default to False, as Scweet does). None = not here."""
    for marker in (f'operationName:"{op}"', f'operationName: "{op}"'):
        i = bundle.find(marker)
        if i < 0:
            continue
        j = bundle.find("featureSwitches:[", i, i + 3000)
        if j < 0:
            return None
        k = bundle.find("]", j)
        names = [n.strip().strip('"') for n in bundle[j + 17:k].split(",") if n.strip()]
        return {n: FEATURES.get(n, False) for n in names} or None
    return None


def indices_ref(bundle: str) -> str | None:
    """The signing-script reference in a bundle. A cheap find first: the regex is lazy and
    unanchored, so it only ever runs on a short window around a candidate."""
    for key in ("ondemand.s", "sign.o"):
        i = bundle.find(key)
        for _ in range(50):
            if i < 0:
                break
            start = max(bundle.rfind(c, max(0, i - 300), i) for c in "\"'`(,") + 1
            if m := INDICES_FILE_RE.search(bundle[start:i + 200]):
                return m.group(0)
            i = bundle.find(key, i + 1)
    return None


def query_id_for(bundle: str, op: str) -> str | None:
    marker = f'",operationName:"{op}"'
    i = bundle.find(marker)
    if i < 0:
        return None
    j = bundle.rfind('queryId:"', max(0, i - 80), i)
    qid = bundle[j + 9:i] if j >= 0 else ""
    return qid if re.fullmatch(r"[\w-]{10,40}", qid) else None


async def _bootstrap(client) -> dict:
    """Load an authenticated x.com page, scan its bundles for the SearchTimeline queryId,
    its feature switches and the signing script, and build the transaction-id generator."""
    record_stage("x.manifest_refresh")
    page = await _page("https://x.com/tesla", client)
    try:
        every = get_scripts_list(page)
    except XClIdError as e:
        raise _classify_page(e) from e
    # The logged-in build lists ~900 chunks (measured 2026-10-30): the signing script is
    # matched by URL over all of them; bodies are fetched main.js first, so the scan
    # usually stops after the first concurrent batch.
    found: dict = {"qid": None, "features": None, "indices": next(
        (s for s in every if INDICES_FILE_RE.search(s)), None)}
    scripts = sorted(every, key=lambda u: "/main." not in u)[:_MAX_BUNDLES]
    sem = asyncio.Semaphore(_BUNDLE_CONCURRENCY)

    async def scan(url: str) -> None:
        async with sem:
            if found["qid"] and found["features"] and found["indices"]:
                return
            try:
                body = (await fetch_static(url, client=client, timeout=_REQ_TIMEOUT_S))["html"]
            except Exception:  # noqa: BLE001 - one missing chunk is not fatal
                return
        found["qid"] = found["qid"] or query_id_for(body, OP)
        found["features"] = found["features"] or features_for(body, OP)
        if not found["indices"] and (ref := indices_ref(body)):
            found["indices"] = urljoin(url, ref)

    await asyncio.gather(*(scan(u) for u in scripts))
    if not found["indices"]:
        raise XError("x_schema_changed", "X signing script not found")
    try:
        indices_js = (await fetch_static(found["indices"], client=client,
                                         timeout=_REQ_TIMEOUT_S))["html"]
        gen = XClIdGen.from_page(page, indices_js)
    except (XClIdError, FetchError) as e:
        raise XError("x_schema_changed", f"transaction id: {type(e).__name__}") from e
    if not found["qid"]:
        record_stage("x.manifest_fallback")
    return {"gen": gen, "qid": found["qid"] or _FALLBACK_QID,
            "features": found["features"] or FEATURES, "at": time.monotonic()}


def _classify_page(e: XClIdError) -> XError:
    if "logged-out" in str(e):
        return XError("x_account_expired", "X did not accept the account cookies")
    return XError("x_schema_changed", "X web app page not recognised")


async def _page(url: str, client) -> str:
    await _get_limiter().acquire()  # an authenticated page load counts against the account
    try:
        res = await fetch_static(url, client=client, timeout=_REQ_TIMEOUT_S, max_redirects=0,
                                 headers=_page_headers())
    except FetchError as e:
        raise _from_status(e.status, "X page") from e
    if res["status"] != 200:
        raise _from_status(res["status"], "X page")
    return res["html"]


def _from_status(status: int | None, what: str) -> XError:
    if status == 429:
        return XError("x_rate_limited", f"{what}: HTTP 429", retry_after=900)
    if status in (401, 302, 301):
        return XError("x_account_expired", f"{what}: HTTP {status} (cookies not accepted)")
    if status == 403:  # body discarded: a stale ct0 (353) and a lock look the same here
        return XError("x_account_rejected", f"{what}: HTTP 403", retry_after=900)
    return XError("x_backend_error", f"{what}: HTTP {status}")


async def _ensure_boot(client, *, force: bool = False) -> dict:
    """The current manifest. Single-flight; a failed bootstrap backs off ``_BOOT_BACKOFF_S``
    (keeping a stale manifest when X itself changed, not the account); a forced refresh of
    a manifest younger than ``_MIN_REFRESH_S`` is refused, so a request X rejects cannot
    turn every call into a page load plus a bundle sweep."""
    global _boot, _boot_fail
    now = time.monotonic()
    if not force and _boot is not None and now - _boot["at"] <= BOOT_TTL_S:
        return _boot
    if force and _boot is not None and now - _boot["at"] < _MIN_REFRESH_S:
        raise XError("x_schema_changed", "X rejected a manifest under 10 minutes old")
    async with _boot_lock():
        if _boot is not None and _boot["at"] > now:  # refreshed while this call waited
            return _boot
        if _boot_fail and time.monotonic() < _boot_fail[0]:
            if _boot is not None and _boot_fail[1] not in _ACCOUNT_CODES:
                return _boot
            raise XError(_boot_fail[1], "X bootstrap failing, backing off",
                         retry_after=int(_boot_fail[0] - time.monotonic()) + 1)
        try:
            _boot = await _bootstrap(client)
        except Exception as e:
            err = e if isinstance(e, XError) else XError(
                "x_backend_error", f"bootstrap: {type(e).__name__}")
            _boot_fail = (time.monotonic() + _BOOT_BACKOFF_S, err.code)
            if err.code in _ACCOUNT_CODES:
                _get_limiter().cool(_BOOT_BACKOFF_S)
            if _boot is not None and err.code not in _ACCOUNT_CODES:
                return _boot  # X changed something; the old manifest may still work
            raise err from e
        _boot_fail = None
    return _boot


async def _call(variables: dict, client, *, retried: bool = False) -> dict:
    boot = await _ensure_boot(client)
    path = f"/i/api/graphql/{boot['qid']}/{OP}"
    url = "https://x.com" + path + "?" + urlencode({
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(boot["features"], separators=(",", ":")),
    })
    limiter = _get_limiter()
    await limiter.acquire()  # before calc: the transaction id carries a timestamp
    headers = {**_cookie_headers(), "content-type": "application/json",
               "x-client-transaction-id": boot["gen"].calc("GET", path)}
    record_stage("x.graphql")
    try:
        res = await fetch_static(url, client=client, timeout=_REQ_TIMEOUT_S, max_redirects=0,
                                 headers=headers, api=True)
        status, text = res["status"], res["html"]
    except FetchError as e:
        status, text = e.status, ""
    if status == 404 and not retried:  # stale queryId or transaction keys: refresh once
        await _ensure_boot(client, force=True)
        return await _call(variables, client, retried=True)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    errors = data.get("errors")
    codes = [e.get("code") for e in (errors if isinstance(errors, list) else [])
             if isinstance(e, dict)]
    for c in codes:
        if c in _X_CODES:
            code, cool = _X_CODES[c]
            if cool:
                limiter.cool(cool)
            raise XError(code, f"X error {c}", retry_after=cool or None)
    if status != 200 or not isinstance(data.get("data"), dict):
        if status in (429, 403):
            limiter.cool(900)
        if status == 400 and not retried:  # usually a renamed feature switch
            await _ensure_boot(client, force=True)
            return await _call(variables, client, retried=True)
        raise (_from_status(status, "SearchTimeline") if status != 200 else
               XError("x_schema_changed", "SearchTimeline answer without data"))
    return data["data"]


# --- parsing --------------------------------------------------------------------------

def _x_time(s: str | None) -> str | None:
    try:
        return datetime.strptime(s or "", "%a %b %d %H:%M:%S %z %Y").astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _unwrap(r: dict | None) -> dict | None:
    if isinstance(r, dict) and r.get("__typename") == "TweetWithVisibilityResults":
        r = r.get("tweet")
    return r if isinstance(r, dict) and isinstance(r.get("legacy"), dict) else None


def _media(m: dict) -> dict:
    url = m.get("media_url_https")
    if m.get("type") in ("video", "animated_gif"):
        variants = [v for v in (m.get("video_info") or {}).get("variants") or []
                    if v.get("content_type") == "video/mp4"]
        if variants:
            url = max(variants, key=lambda v: v.get("bitrate") or 0).get("url")
    return {"type": {"animated_gif": "gif"}.get(m.get("type"), m.get("type")), "url": url}


def post_from_gql(t: dict, *, depth: int = 0) -> dict:
    """GraphQL Tweet result -> Argus post model (same shape as ``post_from_fx``)."""
    lg = t["legacy"]
    user = ((t.get("core") or {}).get("user_results") or {}).get("result") or {}
    uc, ul = user.get("core") or {}, user.get("legacy") or {}
    handle = uc.get("screen_name") or ul.get("screen_name") or ""
    note = (((t.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    text = note.get("text") or lg.get("full_text") or ""
    urls = ((note.get("entity_set") or {}).get("urls") if note else None) \
        or (lg.get("entities") or {}).get("urls") or []
    for u in urls:
        if u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    media = (lg.get("extended_entities") or {}).get("media") or []
    for m in media:  # the trailing t.co link that points at the attached media
        if m.get("url"):
            text = text.replace(m["url"], "")
    views = (t.get("views") or {}).get("count")
    quoted = _unwrap((t.get("quoted_status_result") or {}).get("result"))
    pid = str(t.get("rest_id") or lg.get("id_str") or "")
    return {
        "id": pid,
        "url": f"https://x.com/{handle}/status/{pid}",
        "created_at": _x_time(lg.get("created_at")),
        "text": html.unescape(text).strip(),  # X escapes & < > in full_text
        "lang": lg.get("lang"),
        "author": {
            "handle": handle,
            "name": uc.get("name") or ul.get("name") or "",
            "verified": bool(user.get("is_blue_verified")
                             or (user.get("verification") or {}).get("verified")),
            "followers": (user.get("relationship_counts") or {}).get("followers",
                                                                     ul.get("followers_count")),
        },
        "metrics": {"replies": lg.get("reply_count"), "reposts": lg.get("retweet_count"),
                    "likes": lg.get("favorite_count"), "quotes": lg.get("quote_count"),
                    "views": int(views) if str(views or "").isdigit() else None,
                    "bookmarks": lg.get("bookmark_count")},
        "media": [_media(m) for m in media],
        "quoted": post_from_gql(quoted, depth=1) if quoted and depth == 0 else None,
        "in_reply_to": lg.get("in_reply_to_status_id_str"),
        "source": "graphql",
    }


def parse_search(data: dict) -> tuple[list[dict], str | None]:
    """SearchTimeline ``data`` -> (posts, bottom cursor). Odd entries are skipped, never
    raised: X adds entry kinds (promoted, user modules) without notice."""
    def d(x) -> dict:
        return x if isinstance(x, dict) else {}

    def lst(x) -> list:
        return x if isinstance(x, list) else []

    timeline = d(d(d(d(data).get("search_by_raw_query")).get("search_timeline")).get(
        "timeline"))
    entries: list = []
    for ins in lst(timeline.get("instructions")):
        entries += lst(d(ins).get("entries"))
        entries.append(d(ins).get("entry"))
    posts, cursor = [], None
    for e in entries:
        c = d(d(e).get("content"))
        if c.get("cursorType") == "Bottom" and isinstance(c.get("value"), str):
            cursor = c["value"]
        items = [d(c.get("itemContent"))] + [
            d(d(d(i).get("item")).get("itemContent")) for i in lst(c.get("items"))]
        for ic in items:
            t = _unwrap(d(ic.get("tweet_results")).get("result"))
            if t is None or ic.get("promotedMetadata"):
                continue
            try:
                posts.append(post_from_gql(t))
            except Exception:  # noqa: BLE001, S112 - one odd post must not sink the page
                continue
    return posts, cursor


def raw_query(query: str, since: date | None, until: date | None) -> str:
    """X search syntax; X's ``until:`` is exclusive, ours inclusive."""
    q = query.strip()
    if since:
        q += f" since:{since.isoformat()}"
    if until:
        q += f" until:{(until + timedelta(days=1)).isoformat()}"
    return q


async def search_posts(query: str, *, mode: str, limit: int, since: date | None,
                       until: date | None, client) -> dict:
    """``{results, source: "graphql", degraded, degraded_reason}``. Raises XError, after
    recording the account state for /health."""
    global _state
    variables = {"rawQuery": raw_query(query, since, until), "count": 20,
                 "product": "Latest" if mode == "latest" else "Top",
                 "querySource": "typed_query"}
    posts: list[dict] = []
    seen: set[str] = set()
    try:
        for _ in range(_MAX_PAGES):
            page, cursor = parse_search(await _call(variables, client))
            fresh = [p for p in page if p["id"] not in seen]
            seen.update(p["id"] for p in fresh)
            posts += fresh
            if len(posts) >= limit or not fresh or not cursor:
                break
            variables = {**variables, "cursor": cursor}
    except XError as e:
        _state = _STATE_OF.get(e.code, "error")
        raise
    except Exception as e:  # a parse or transport surprise is an error state, not "ok"
        _state = "error"
        raise XError("x_backend_error", f"SearchTimeline: {type(e).__name__}") from e
    _state = "ok"
    return {"results": posts[:limit], "source": "graphql", "degraded": False,
            "degraded_reason": None}
