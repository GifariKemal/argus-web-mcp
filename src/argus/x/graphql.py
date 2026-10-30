"""X Layer 1: X web GraphQL with one cookie account (opt-in): SearchTimeline for
``x_search``, TweetDetail and UserByScreenName + UserTweets for ``read`` on x.com.

Active only when ARGUS_X_AUTH_TOKEN and ARGUS_X_CT0 are set. The queryId and feature
switches are read live from X's web bundles (idea from Scweet, MIT) with a pinned fallback,
so an X-side rotation heals on the next refresh instead of needing a release. Every call
is paced by ``Limiter`` and goes out from the server IP through the SSRF-guarded client
with ``max_redirects=0``, so the cookies never follow a redirect to another host. Cookie
values never appear in errors, logs or results.

Fallback queryIds and features come from twscrape (MIT, (c) 2023 vladkens), commit 63f61a5.
X rotates ``ct0`` with Set-Cookie; the new value is kept in memory and in
``~/.argus/x_session.json`` (mode 600), bound to the env cookies it was rotated from, so a
restart keeps it and freshly copied cookies replace it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import os
import random
import re
import tempfile
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urljoin

from .. import config
from ..fetch.static import FetchError, fetch_static
from ..models import record_stage
from .fxtwitter import post_block, x_target
from .xclid import INDICES_FILE_RE, XClIdError, XClIdGen, get_scripts_list

# Pinned fallbacks; the live manifest from main.js wins (on 2026-10-30 the pinned
# SearchTimeline id was already stale).
_FALLBACK_QIDS = {"SearchTimeline": "BYppgupFFS2yqVmmRNjlsw",
                  "TweetDetail": "wjV7NuWEsHgTBFfW3zYKzQ",
                  "UserByScreenName": "AMIBMjtxEEATh4z8V9GtRg",
                  "UserTweets": "OdBLACRZ0-9NikpJotaT7g"}
OPS = tuple(_FALLBACK_QIDS)
_ARTICLE_TOGGLES = {"withArticleRichContentState": True, "withArticlePlainText": False,
                    "withArticleSummaryText": True, "withArticleVoiceOver": True,
                    "withGrokAnalyze": False, "withDisallowedReplyControls": False}
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
# Locked or expired also cools the account: re-sending its cookies on every read would
# only make a lock worse, and fresh cookies need a restart anyway.
_X_CODES = {88: ("x_rate_limited", 900), 32: ("x_account_expired", 900),
            89: ("x_account_expired", 900), 215: ("x_account_expired", 900),
            353: ("x_account_expired", 900), 64: ("x_account_locked", 900),
            141: ("x_account_locked", 900), 326: ("x_account_locked", 900)}
_STATE_OF = {"x_rate_limited": "rate_limited", "x_account_expired": "expired",
             "x_account_locked": "locked", "x_account_rejected": "rejected",
             "x_schema_changed": "schema_changed", "x_backend_error": "error"}
_ACCOUNT_CODES = frozenset({"x_rate_limited", "x_account_expired", "x_account_locked",
                            "x_account_rejected"})
# A failed bootstrap is not retried for this long, so a dead account does not load an
# authenticated page on every call; a forced refresh needs a manifest at least this old.
_BOOT_BACKOFF_S = 900
_MIN_REFRESH_S = 600
# Reads stop at this share of the per-window and per-day budget, so x_search always has
# room; past it, read on x.com uses FxTwitter.
_READ_SHARE = 0.6
_READ_TIMEOUT_S = 15
# Bundles fetched after the signing script and the search/thread ops are found before the
# sweep gives up on the rest (they keep their pinned fallback).
_EXTRA_BUNDLES = 30


class XError(Exception):
    """A Layer 1 failure. ``code`` is an Argus error code; ``retry_after`` in seconds."""

    def __init__(self, code: str, message: str, retry_after: int | None = None, *,
                 internal: bool = False) -> None:
        self.code = code
        self.retry_after = retry_after
        self.internal = internal  # Argus's own pacing, not something X said
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

    def has_room(self, share: float) -> bool:
        """No cooldown, and under ``share`` of the window and daily budgets."""
        now = self.clock()
        recent = sum(1 for t in self.calls if now - t < self.window_s)
        day = sum(1 for t in self.calls if now - t < 86400)
        return (now >= self.cooldown_until and recent < self.per_window * share
                and day < self.per_day * share)

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
                             retry_after=int(wait) + 1, internal=True)
            gap = 1.0 + random.uniform(0, 2) - (self.clock() - self.last)  # noqa: S311 - jitter
            if gap > 0:
                await asyncio.sleep(gap)
            self.last = self.clock()
            self.calls.append(self.last)


_limiter: Limiter | None = None
_boot: dict | None = None  # {"gen", "ops": {op: (queryId, features)}, "at"}
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


SESSION_PATH = Path("~/.argus/x_session.json").expanduser()
_CT0_SET = re.compile(r"\s*ct0=([A-Za-z0-9%_.-]{16,512})(?:;|$)")
_ct0_live: str | None = None  # rotated by X, valid for the current env cookies
_session_loaded = False


def _tag(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _ct0() -> str:
    """The csrf cookie to send: X's latest rotation if it belongs to the env cookies."""
    global _ct0_live, _session_loaded
    if not _session_loaded:
        _session_loaded = True
        try:
            saved = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
            if (saved.get("auth") == _tag(config.X_AUTH_TOKEN)
                    and saved.get("env_ct0") == _tag(config.X_CT0)
                    and _CT0_SET.fullmatch(f"ct0={saved.get('ct0')}")):
                _ct0_live = saved["ct0"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # no or unreadable file: the env value stands
    return _ct0_live or config.X_CT0


def _remember_ct0(set_cookies) -> None:
    """Keep a ct0 that X rotated via Set-Cookie (memory + 600 file). Never raises."""
    global _ct0_live
    for sc in set_cookies or []:
        m = _CT0_SET.match(sc) if isinstance(sc, str) else None
        if not m or m.group(1) == _ct0():
            continue
        _ct0_live = m.group(1)
        record_stage("x.ct0_rotated")
        data = json.dumps({"auth": _tag(config.X_AUTH_TOKEN), "env_ct0": _tag(config.X_CT0),
                           "ct0": _ct0_live})
        tmp = None
        try:
            SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=SESSION_PATH.parent, suffix=".tmp")  # mode 600
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, SESSION_PATH)
        except OSError:  # memory still has it; the file is only for restarts
            if tmp and os.path.exists(tmp):
                with contextlib.suppress(OSError):
                    os.unlink(tmp)


def _cookie() -> str:
    return f"auth_token={config.X_AUTH_TOKEN}; ct0={_ct0()}"


def _page_headers() -> dict:
    """What a browser sends for the x.com page: the session cookie, no API headers."""
    return {"user-agent": _UA, "cookie": _cookie()}


def _cookie_headers() -> dict:
    return {
        "user-agent": _UA,
        "authorization": _BEARER,
        "cookie": _cookie(),
        "x-csrf-token": _ct0(),
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
    """Load an authenticated x.com page, scan its bundles for each operation's queryId and
    feature switches and for the signing script, and build the transaction-id generator."""
    record_stage("x.manifest_refresh")
    page = await _page("https://x.com/tesla", client)
    try:
        every = get_scripts_list(page)
    except XClIdError as e:
        raise _classify_page(e) from e
    # The logged-in build lists ~900 chunks (measured 2026-10-30): the signing script is
    # matched by URL over all of them; bodies are fetched main.js first, so the scan
    # usually stops after the first concurrent batch.
    qids: dict = {}
    feats: dict = {}
    found: dict = {"indices": next((s for s in every if INDICES_FILE_RE.search(s)), None)}

    scanned = {"after_core": 0}

    def done() -> bool:
        if not found["indices"] or not all(
                op in qids and op in feats for op in ("SearchTimeline", "TweetDetail")):
            return False
        return (all(op in qids and op in feats for op in OPS)
                or scanned["after_core"] >= _EXTRA_BUNDLES)

    scripts = sorted(every, key=lambda u: "/main." not in u)[:_MAX_BUNDLES]
    sem = asyncio.Semaphore(_BUNDLE_CONCURRENCY)

    async def scan(url: str) -> None:
        async with sem:
            if done():
                return
            if found["indices"] and "SearchTimeline" in qids and "TweetDetail" in qids:
                scanned["after_core"] += 1
            try:
                body = (await fetch_static(url, client=client, timeout=_REQ_TIMEOUT_S))["html"]
            except Exception:  # noqa: BLE001 - one missing chunk is not fatal
                return
        for op in OPS:
            if op not in qids and (q := query_id_for(body, op)):
                qids[op] = q
            if op not in feats and (f := features_for(body, op)):
                feats[op] = f
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
    if len(qids) < len(OPS):
        record_stage("x.manifest_fallback")
    return {"gen": gen, "at": time.monotonic(),
            "ops": {op: (qids.get(op, _FALLBACK_QIDS[op]), feats.get(op, FEATURES))
                    for op in OPS}}


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
    _remember_ct0(res.get("set_cookie"))
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


async def _call(op: str, variables: dict, client, *, toggles: dict | None = None,
                retried: bool = False, refresh: bool = True) -> dict:
    """One GraphQL call. ``refresh=False`` (the read path) never bootstraps or refreshes the
    manifest: a cancelled read must not leave a half-done bootstrap behind."""
    if not refresh and (_boot is None or time.monotonic() - _boot["at"] > BOOT_TTL_S):
        raise XError("x_schema_changed", "no fresh manifest yet", internal=True)
    boot = _boot if not refresh else await _ensure_boot(client)
    qid, features = boot["ops"][op]
    path = f"/i/api/graphql/{qid}/{op}"
    params = {"variables": json.dumps(variables, separators=(",", ":")),
              "features": json.dumps(features, separators=(",", ":"))}
    if toggles:
        params["fieldToggles"] = json.dumps(toggles, separators=(",", ":"))
    url = "https://x.com" + path + "?" + urlencode(params)
    limiter = _get_limiter()
    await limiter.acquire()  # before calc: the transaction id carries a timestamp
    headers = {**_cookie_headers(), "content-type": "application/json",
               "x-client-transaction-id": boot["gen"].calc("GET", path)}
    record_stage("x.graphql")
    try:
        res = await fetch_static(url, client=client, timeout=_REQ_TIMEOUT_S, max_redirects=0,
                                 headers=headers, api=True)
        status, text = res["status"], res["html"]
        _remember_ct0(res.get("set_cookie"))
    except FetchError as e:
        status, text = e.status, ""
    if status == 404 and not retried and refresh:  # stale queryId or keys: refresh once
        await _ensure_boot(client, force=True)
        return await _call(op, variables, client, toggles=toggles, retried=True)
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
        if status == 400 and not retried and refresh:  # usually a renamed feature switch
            await _ensure_boot(client, force=True)
            return await _call(op, variables, client, toggles=toggles, retried=True)
        raise (_from_status(status, op) if status != 200 else
               XError("x_schema_changed", f"{op} answer without data"))
    return data["data"]


# --- parsing --------------------------------------------------------------------------

def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _lst(x) -> list:
    return x if isinstance(x, list) else []


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


class Protected(ValueError):
    """The author's account is protected: its posts are never returned."""


def _protected(user: dict) -> bool:
    return bool(_d(user.get("privacy")).get("protected") or _d(user.get("legacy")).get(
        "protected"))


def post_from_gql(t: dict, *, depth: int = 0) -> dict:
    """GraphQL Tweet result -> Argus post model (same shape as ``post_from_fx``). A repost
    becomes the original post plus ``reposted_by``. Raises Protected for a protected
    author: the account may follow them, but their posts are not ours to hand out."""
    lg = t["legacy"]
    user = ((t.get("core") or {}).get("user_results") or {}).get("result") or {}
    if _protected(user):
        raise Protected("protected account")
    uc, ul = user.get("core") or {}, user.get("legacy") or {}
    if depth == 0 and (rt := _unwrap(_d(lg.get("retweeted_status_result")).get("result"))):
        original = post_from_gql(rt)
        return {**original, "reposted_by": uc.get("screen_name") or ul.get("screen_name")}
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
    if quoted and _protected(_d(_d(_d(quoted.get("core")).get("user_results")).get("result"))):
        quoted = None
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
    """SearchTimeline ``data`` -> (posts, bottom cursor)."""
    return parse_timeline(_d(_d(_d(_d(data).get("search_by_raw_query")).get(
        "search_timeline")).get("timeline")))


def parse_timeline(timeline: dict) -> tuple[list[dict], str | None]:
    """Any GraphQL timeline (``{instructions: [...]}``) -> (posts in order, bottom cursor),
    deduped by id. Odd entries are skipped, never raised: X adds entry kinds (promoted,
    user modules, cursors) without notice."""
    d, lst = _d, _lst
    timeline = d(timeline)
    entries: list = []
    for ins in lst(timeline.get("instructions")):
        entries += lst(d(ins).get("entries"))
        entries.append(d(ins).get("entry"))
    posts, cursor = [], None
    seen: set[str] = set()
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
                p = post_from_gql(t)
            except Exception:  # noqa: BLE001, S112 - one odd post must not sink the page
                continue
            if p["id"] not in seen:
                seen.add(p["id"])
                posts.append(p)
    return posts, cursor


def parse_thread(data: dict, focal_id: str) -> dict | None:
    """TweetDetail ``data`` -> the focal post plus ``thread`` (posts above it) and
    ``replies`` (below it), or None when the post is not in the answer."""
    posts, _ = parse_timeline(_d(_d(data).get("threaded_conversation_with_injections_v2")))
    idx = next((i for i, p in enumerate(posts) if p["id"] == focal_id), None)
    if idx is None:
        return None
    return {**posts[idx], "thread": posts[:idx], "replies": posts[idx + 1:]}


def parse_user(data: dict) -> dict | None:
    """UserByScreenName ``data`` -> profile model (same keys as FxTwitter's) + ``id`` and
    ``created_at``, or None for an unavailable or suspended account."""
    r = _d(_d(_d(data).get("user")).get("result"))
    if r.get("__typename") != "User" or not r.get("rest_id") or _protected(r):
        return None
    core, lg = _d(r.get("core")), _d(r.get("legacy"))
    counts, tc, bio = _d(r.get("relationship_counts")), _d(r.get("tweet_counts")), _d(
        r.get("profile_bio"))
    handle = core.get("screen_name") or lg.get("screen_name") or ""
    desc = bio.get("description") or lg.get("description") or ""
    for u in _lst(_d(_d(bio.get("entities")).get("description")).get("urls")):
        if _d(u).get("url") and u.get("expanded_url"):
            desc = desc.replace(u["url"], u["expanded_url"])
    return {
        "handle": handle,
        "name": core.get("name") or lg.get("name") or "",
        "verified": bool(r.get("is_blue_verified") or _d(r.get("verification")).get("verified")),
        "followers": counts.get("followers", lg.get("followers_count")),
        "following": counts.get("following", lg.get("friends_count")),
        "posts": tc.get("tweets", lg.get("statuses_count")),
        "description": html.unescape(desc),
        "url": f"https://x.com/{handle}",
        "id": str(r["rest_id"]),
        "created_at": _x_time(core.get("created_at") or lg.get("created_at")),
    }


def parse_user_tweets(data: dict) -> list[dict]:
    r = _d(_d(_d(data).get("user")).get("result"))
    tl = _d(r.get("timeline") or r.get("timeline_v2"))
    return parse_timeline(_d(tl.get("timeline")))[0]


def raw_query(query: str, since: date | None, until: date | None) -> str:
    """X search syntax; X's ``until:`` is exclusive, ours inclusive."""
    q = query.strip()
    if since:
        q += f" since:{since.isoformat()}"
    if until:
        q += f" until:{(until + timedelta(days=1)).isoformat()}"
    return q


async def _tracked(what: str, coro):
    """Await ``coro``, recording the account state for /health. Raises XError only."""
    global _state
    try:
        result = await coro
    except XError as e:
        if not e.internal:
            _state = _STATE_OF.get(e.code, "error")
        raise
    except Exception as e:  # a parse or transport surprise is an error state, not "ok"
        _state = "error"
        raise XError("x_backend_error", f"{what}: {type(e).__name__}") from e
    _state = "ok"
    return result


async def search_posts(query: str, *, mode: str, limit: int, since: date | None,
                       until: date | None, client) -> dict:
    """``{results, source: "graphql", degraded, degraded_reason}``. Raises XError."""
    async def run() -> list[dict]:
        variables = {"rawQuery": raw_query(query, since, until), "count": 20,
                     "product": "Latest" if mode == "latest" else "Top",
                     "querySource": "typed_query"}
        posts: list[dict] = []
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            page, cursor = parse_search(await _call("SearchTimeline", variables, client))
            fresh = [p for p in page if p["id"] not in seen]
            seen.update(p["id"] for p in fresh)
            posts += fresh
            if len(posts) >= limit or not fresh or not cursor:
                break
            variables = {**variables, "cursor": cursor}
        return posts

    posts = await _tracked("SearchTimeline", run())
    return {"results": posts[:limit], "source": "graphql", "degraded": False,
            "degraded_reason": None}


async def read_status(post_id: str, client) -> dict | None:
    """One post with its thread and first replies page (TweetDetail). Raises XError; None
    when X has no such post."""
    async def run():
        data = await _call("TweetDetail", {
            "focalTweetId": post_id, "with_rux_injections": False,
            "includePromotedContent": False, "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True, "withBirdwatchNotes": True,
            "withVoice": True, "withV2Timeline": True}, client, toggles=_ARTICLE_TOGGLES,
            refresh=False)
        return parse_thread(data, post_id)

    return await _tracked("TweetDetail", run())


async def read_profile(handle: str, client) -> dict | None:
    """Profile plus its latest posts (UserByScreenName + UserTweets: two calls). Raises
    XError; None when the account is unavailable."""
    async def run():
        user = parse_user(await _call("UserByScreenName", {
            "screen_name": handle, "withSafetyModeUserFields": True}, client, refresh=False))
        if user is None:
            return None
        data = await _call("UserTweets", {
            "userId": user["id"], "count": 20, "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True, "withVoice": True,
            "withV2Timeline": True}, client, refresh=False)
        return {**user, "posts_latest": parse_user_tweets(data)}

    return await _tracked("UserByScreenName", run())


def _render(kind: str, obj: dict) -> str:
    """A minimal page for the extractor; ``post_block`` escapes every field."""
    e = html.escape
    if kind == "status":
        title = f"@{obj['author']['handle']}: {obj['text'][:80]}"
        parts = []
        if obj["thread"]:
            parts += ["<h2>Thread</h2>"] + [post_block(p) for p in obj["thread"]]
        parts.append(post_block(obj))
        if obj["replies"]:
            parts += ["<h2>Replies</h2>"] + [post_block(p) for p in obj["replies"]]
    else:
        title = f"{obj['name']} (@{obj['handle']})"
        parts = [f"<h1>{e(title)}</h1><p>{e(obj['description'])}</p>",
                 f"<p>followers {e(str(obj['followers']))}, following "
                 f"{e(str(obj['following']))}, posts {e(str(obj['posts']))}</p>"]
        if obj["posts_latest"]:
            parts += ["<h2>Posts</h2>"] + [post_block(p) for p in obj["posts_latest"]]
    return (f"<html><head><title>{e(title)}</title></head><body><article>{''.join(parts)}"
            f"</article></body></html>")


_warm_task: asyncio.Task | None = None


async def _warm(client) -> None:
    with contextlib.suppress(Exception):
        await _ensure_boot(client)


def _schedule_warm(client) -> None:
    """Build the manifest in the background (its own task, so no read timeout cancels it
    halfway); reads use FxTwitter until it is ready."""
    global _warm_task
    if _warm_task is None or _warm_task.done():
        _warm_task = asyncio.get_running_loop().create_task(_warm(client))


async def fetch_x_account(url: str, *, client, timeout: float = _READ_TIMEOUT_S) -> dict | None:
    """An X post (with thread and replies) or profile (with latest posts) through the
    account, as a fetch result like ``fetch_x``; None when Layer 1 is off, the URL is not
    X, the manifest is not ready, the account is cooling or past the read share of its
    budget, or anything failed (the caller then uses FxTwitter). Never raises."""
    target = x_target(url)
    if target is None or not enabled():
        return None
    if _boot is None or time.monotonic() - _boot["at"] > BOOT_TTL_S:
        _schedule_warm(client)
        return None
    if not _get_limiter().has_room(_READ_SHARE):
        record_stage("x.read_budget")
        return None
    kind, ident = target
    try:
        async with asyncio.timeout(min(timeout, _READ_TIMEOUT_S)):
            obj = await (read_status(ident, client) if kind == "status"
                         else read_profile(ident, client))
        page = _render(kind, obj) if obj else None
    except Exception:  # noqa: BLE001 - XError, timeout or an odd shape: FxTwitter takes over
        return None
    if page is None:
        return None
    return {"final_url": url, "status": 200, "html": page, "render_path": "api",
            "structured": obj}
