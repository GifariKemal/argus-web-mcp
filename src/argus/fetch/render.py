"""Browser render tier - a single shared Chromium (Crawl4AI) + a semaphore.

One browser is launched in the server lifespan; each render uses a fresh page,
bounded by an asyncio.Semaphore (RAM guard). Browser-tier SSRF is best-effort:
we resolve+validate the host before navigating, but Chromium does its own DNS so
this does not pin against rebinding the way the httpx tier does.
ponytail: acceptable P1 ceiling - pin/proxy the browser only if a concrete need appears.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from ..security.ssrf import aresolve_and_validate, validate_url
from .static import _DEFAULT_PORTS, FetchError

# Markers of an anti-bot interstitial (Cloudflare / Akamai / PerimeterX challenge pages).
_BLOCK_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "attention required",
    "access denied",
    "enable javascript and cookies",
    "verify you are human",
)
_BLOCK_STATUSES = {403, 429, 503}

# Outer wall-clock grace on top of Crawl4AI's own page_timeout: if Playwright/CDP wedges
# (browser crash, hung pipe) page_timeout never fires and the semaphore permit would be
# held forever - 4 wedged renders permanently kill the browser tier. Monkeypatchable in tests.
_RENDER_GRACE_S = 15.0


def _looks_blocked(html: str, status: int | None) -> bool:
    """Heuristic: does this response look like an anti-bot challenge rather than content?"""
    if status in _BLOCK_STATUSES:
        return True
    head = (html or "")[:4000].lower()
    return any(m in head for m in _BLOCK_MARKERS)


class BrowserPool:
    def __init__(self, concurrency: int = 4) -> None:
        self._crawler = None
        self._stealth = None  # lazy stealth crawler - started only on first anti-bot block
        self._concurrency = concurrency
        self._sem = asyncio.Semaphore(concurrency)
        self._stealth_lock = asyncio.Lock()  # serialize lazy stealth init (audit R5)

    @property
    def active_contexts(self) -> int:
        """In-flight pages = concurrency minus available semaphore permits (OOM early-warning)."""
        return self._concurrency - self._sem._value

    async def start(self) -> None:
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        self._crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        await self._crawler.start()

    async def stop(self) -> None:
        for attr in ("_crawler", "_stealth"):
            c = getattr(self, attr)
            if c is not None:
                await c.close()
                setattr(self, attr, None)

    async def _ensure_stealth(self):
        """Lazily start a stealth Chromium (Crawl4AI enable_stealth -> Patchright tier).

        Lock + double-check so two concurrent blocked renders start it exactly once
        (else one Chromium leaks - audit R5).
        """
        if self._stealth is None:
            async with self._stealth_lock:
                if self._stealth is None:
                    from crawl4ai import AsyncWebCrawler, BrowserConfig

                    crawler = AsyncWebCrawler(
                        config=BrowserConfig(
                            headless=True, verbose=False, enable_stealth=True
                        )
                    )
                    await crawler.start()
                    self._stealth = crawler
        return self._stealth

    async def _bounded_arun(self, crawler, url: str, cfg, timeout: float):
        """``crawler.arun`` with an outer stdlib deadline so a wedged Chromium/CDP pipe
        cannot hold a semaphore permit forever. Crawl4AI's page_timeout stays the primary
        mechanism; the +_RENDER_GRACE_S outer bound fires only when it already failed to."""
        try:
            async with asyncio.timeout(timeout + _RENDER_GRACE_S):
                return await crawler.arun(url, config=cfg)
        except TimeoutError as e:
            raise FetchError(
                "render_failed",
                f"render exceeded {timeout + _RENDER_GRACE_S:.0f}s (browser wedged?)",
            ) from e

    async def render(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        actions: list | None = None,
        screenshot: bool = False,
        timeout: float = 45,
        stealth: bool = False,
    ) -> dict:
        """Render ``url`` in a fresh page. Escalates to the stealth tier on an anti-bot block.

        Returns ``{final_url, html, screenshot, render_tier}``. Screenshots are full-page
        (Crawl4AI default); a viewport-clip path is intentionally not wired (ponytail: add when
        a concrete need appears; Crawl4AI viewport is browser-level).
        """
        from crawl4ai import CacheMode, CrawlerRunConfig

        validate_url(url)
        parts = urlsplit(url)
        await aresolve_and_validate(parts.hostname, parts.port or _DEFAULT_PORTS[parts.scheme])

        if self._crawler is None:
            raise FetchError("render_failed", "browser pool not started")

        cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            screenshot=screenshot,
            wait_for=wait_for,
            page_timeout=int(timeout * 1000),
            js_code=actions or None,
        )

        crawler = await self._ensure_stealth() if stealth else self._crawler
        tier = "stealth" if stealth else "normal"
        async with self._sem:
            res = await self._bounded_arun(crawler, url, cfg, timeout)

        # Auto-escalate to the stealth tier once on an anti-bot block.
        blocked = not res.success or _looks_blocked(
            getattr(res, "html", ""), getattr(res, "status_code", None)
        )
        if not stealth and blocked:
            stealth_crawler = await self._ensure_stealth()
            async with self._sem:
                res2 = await self._bounded_arun(stealth_crawler, url, cfg, timeout)
            # Adopt the stealth result only if it is BOTH successful and not itself a
            # challenge page - a 200 "Just a moment..." must not replace res silently.
            if res2.success and not _looks_blocked(
                getattr(res2, "html", ""), getattr(res2, "status_code", None)
            ):
                res, tier = res2, "stealth"

        if not res.success:
            still_blocked = _looks_blocked(
                getattr(res, "html", ""), getattr(res, "status_code", None)
            )
            code = "blocked_by_antibot" if still_blocked else "render_failed"
            raise FetchError(code, res.error_message or "render failed")
        # Truthfulness gate: a success=True result that is still a challenge page (both
        # tiers blocked, or the direct stealth path hit a wall) must surface as
        # blocked_by_antibot - never as content. Callers (fetch core) then fall through
        # to their static/Wayback ladder instead of extracting "Verify you are human".
        # EXCEPTION: a screenshot request returns whatever was captured - "show me what
        # the page looks like" is legitimate even for a challenge page, and the PNG is
        # already in hand.
        if not screenshot and _looks_blocked(
            getattr(res, "html", ""), getattr(res, "status_code", None)
        ):
            raise FetchError(
                "blocked_by_antibot", "challenge page persists after stealth escalation"
            )
        return {
            "final_url": res.url or url,
            "html": res.html,
            "screenshot": res.screenshot if screenshot else None,
            "render_tier": tier,
        }
