"""Tiered fetch orchestration: static fast-path, escalate to browser on thin/JS.

Cheap -> expensive: httpx static GET -> (if content looks thin/JS-rendered) browser.
Thin detection uses a cheap visible-text heuristic so we don't pay extraction cost
twice. SSRF is enforced in the static hop guard and the browser pre-check.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from ..models import record_stage
from ..security.ssrf import SSRFError
from .fallback import fetch_via_archive
from .render import BrowserPool
from .static import FetchError, _guard, fetch_static

logger = logging.getLogger("argus.fetch")

# Below this many chars of visible (non-script) text, a static page is treated as
# JS-rendered/thin and escalated to the browser tier.
# ponytail: heuristic, not extraction - names the ceiling; tune if it mis-escalates.
ESCALATE_BELOW_CHARS = 200
STATIC_FALLBACK_TIMEOUT = 5

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


def _visible_text_len(html: str) -> int:
    """Char count of visible text (scripts/styles/tags removed) - JS-shell detector."""
    stripped = _SCRIPT_STYLE.sub(" ", html)
    return len(" ".join(_TAGS.sub(" ", stripped).split()))


async def fetch(url: str, *, throttle=None, **kwargs) -> dict:
    """Fetch ``url`` (see ``_do_fetch``) with an optional per-host courtesy/circuit-breaker
    ``throttle`` (a HostThrottle). None = no throttling (default; tests). Raises SSRFError /
    FetchError; an open circuit surfaces as FetchError."""
    if throttle is None:
        return await _do_fetch(url, **kwargs)
    host = urlsplit(url).hostname or ""
    from .throttle import CircuitOpen

    try:
        await throttle.acquire(host)
    except CircuitOpen as e:
        raise FetchError("fetch_failed", f"circuit open for {host}") from e
    try:
        result = await _do_fetch(url, **kwargs)
    except (FetchError, SSRFError):
        throttle.record_failure(host)
        raise
    throttle.record_success(host)
    return result


async def _do_fetch(
    url: str,
    *,
    render: bool = False,
    wait_for: str | None = None,
    actions: list | None = None,
    screenshot: bool = False,
    timeout: float = 30,
    client=None,
    browser: BrowserPool | None = None,
) -> dict:
    """Tiered fetch. Returns ``{final_url, status, html, render_path, screenshot?}``.

    Raises SSRFError (blocked) or FetchError (transport/render). Never returns silently
    truncated content.
    """
    force_browser = render or screenshot or bool(actions) or bool(wait_for)
    if force_browser:
        if browser is None:
            raise FetchError("render_failed", "render requested but no browser available")
        await _guard(url)  # SSRF resolve-then-validate before navigating (browser tier too)
        record_stage("fetch.forced_browser")
        logger.debug("fetch[%s]: forced browser render", url)
        r = await browser.render(
            url, wait_for=wait_for, actions=actions, screenshot=screenshot,
            timeout=max(timeout, 45),
        )
        return {
            "final_url": r["final_url"],
            "status": 200,
            "html": r["html"],
            "render_path": "browser",
            "screenshot": r.get("screenshot"),
            "render_tier": r.get("render_tier", "normal"),
        }

    static_timeout = min(timeout, STATIC_FALLBACK_TIMEOUT) if browser is not None else timeout
    try:
        res = await fetch_static(url, client=client, timeout=static_timeout)
    except FetchError as exc:
        # Transport/connect/timeout - the host is unreachable/blocked from this box.
        # SSRFError is a different type and is NOT caught here: a blocked URL must
        # propagate without any fallback attempt. Recover via server-side mirrors.
        record_stage("fetch.static_fail")
        logger.info("fetch[%s]: static hop failed (%s); trying fallbacks", url, exc)
        # 1) stealth browser tier (may route/behave differently than the httpx hop).
        if browser is not None:
            try:
                r = await browser.render(url, stealth=True, timeout=max(timeout, 45))
            except FetchError as rexc:
                record_stage("fetch.fallback_stealth_fail")
                logger.info("fetch[%s]: stealth fallback failed (%s)", url, rexc)
            else:
                record_stage("fetch.fallback_stealth_ok")
                logger.info("fetch[%s]: recovered via stealth browser", url)
                return {
                    "final_url": r["final_url"],
                    "status": 200,
                    "html": r["html"],
                    "render_path": "browser",
                    "screenshot": None,
                    "render_tier": r.get("render_tier", "stealth"),
                }
        # 2) latest Wayback Machine snapshot (never raises; None if no snapshot).
        archived = await fetch_via_archive(url, client=client, timeout=timeout)
        if archived is not None:
            record_stage("fetch.fallback_archive_ok")
            logger.info("fetch[%s]: recovered via Wayback archive", url)
            return archived
        # 3) all fallbacks exhausted - surface the original transport failure.
        record_stage("fetch.fallback_exhausted")
        logger.warning("fetch[%s]: all fallbacks exhausted; raising transport failure", url)
        raise exc

    if browser is not None and _visible_text_len(res["html"]) < ESCALATE_BELOW_CHARS:
        record_stage("fetch.thin_escalate")
        logger.info("fetch[%s]: thin static content (<%d chars); escalating to browser",
                    url, ESCALATE_BELOW_CHARS)
        try:
            r = await browser.render(url, timeout=max(timeout, 45))
        except FetchError as rexc:
            record_stage("fetch.thin_escalate_fail")
            logger.info("fetch[%s]: escalation failed (%s); keeping thin static result",
                        url, rexc)
            return res  # ponytail: keep the thin static result rather than failing the read
        record_stage("fetch.thin_escalate_ok")
        return {
            "final_url": r["final_url"],
            "status": res["status"],
            "html": r["html"],
            "render_path": "browser",
            "screenshot": None,
        }
    record_stage("fetch.static_ok")
    return res
