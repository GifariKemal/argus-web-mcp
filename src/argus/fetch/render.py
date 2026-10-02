"""Browser render tier - a single shared Chromium (Crawl4AI) + a semaphore.

One browser is launched in the server lifespan; each render uses a fresh page,
bounded by an asyncio.Semaphore (RAM guard). Every Chromium connection - navigation,
redirect hop, subresource, page JS, WebSocket - goes through the loopback egress proxy
(security/egress.py), which runs the same SSRF gate as the httpx tier and dials the
validated IP. Before 0.4.20 only the seed URL was checked, and a public 302 to an internal
service came back through scrape.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from ..security.egress import guarded_browser_config
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

# Playwright/crawl4ai error text once the Chromium process is gone (crash, OOM kill).
_DEAD_MARKERS = ("has been closed", "browser has disconnected", "target closed")


def _browser_dead(res) -> bool:
    msg = (getattr(res, "error_message", "") or "").lower()
    return not getattr(res, "success", True) and any(m in msg for m in _DEAD_MARKERS)


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
        self._restart_lock = asyncio.Lock()  # one relaunch even if many renders see it die

    @property
    def active_contexts(self) -> int:
        """In-flight pages = concurrency minus available semaphore permits (OOM early-warning)."""
        return self._concurrency - self._sem._value

    async def start(self) -> None:
        from crawl4ai import AsyncWebCrawler

        # Started before it is published: a render must never see a half-started crawler
        # (crawl4ai would start it a second time and leak a Chromium).
        crawler = AsyncWebCrawler(
            config=await guarded_browser_config(headless=True, verbose=False)
        )
        await crawler.start()
        self._crawler = crawler

    def alive(self, crawler=None) -> bool:
        """False once the Chromium behind ``crawler`` (default: the shared one) has exited.
        Unknown -> True: a crawl4ai internals change must not flip health by itself."""
        crawler = self._crawler if crawler is None else crawler
        browser = getattr(getattr(getattr(crawler, "crawler_strategy", None),
                                  "browser_manager", None), "browser", None)
        is_connected = getattr(browser, "is_connected", None)
        return crawler is not None and (is_connected is None or bool(is_connected()))

    async def _restart_normal(self, dead) -> None:
        """Relaunch the shared Chromium after it died. Before 0.4.21 a crash or OOM kill
        left every render failing until the next deploy, with /health still ok."""
        async with self._restart_lock:
            if self._crawler is not dead:  # another render already relaunched it
                return
            try:
                async with asyncio.timeout(10):  # a hung close must not pin the lock
                    await dead.close()
            except Exception:  # noqa: BLE001, S110 - the process is already gone
                pass
            await self.start()

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
                    from crawl4ai import AsyncWebCrawler

                    crawler = AsyncWebCrawler(
                        config=await guarded_browser_config(
                            headless=True, verbose=False, enable_stealth=True
                        )
                    )
                    await crawler.start()
                    self._stealth = crawler
        return self._stealth

    async def _recycle_stealth(self) -> None:
        """Close and drop a wedged stealth crawler so the next call re-inits a fresh one.

        Only the STEALTH tier is recycled: it has a lazy re-init path (_ensure_stealth),
        whereas the normal _crawler has none - nulling it would make render() raise
        "browser pool not started" until lifespan restart. Best-effort; close errors are
        swallowed (the handle is already presumed wedged)."""
        async with self._stealth_lock:
            crawler, self._stealth = self._stealth, None
        if crawler is not None:
            try:
                await crawler.close()
            except Exception:  # noqa: BLE001, S110 - handle is wedged; nothing better to do
                pass

    async def _bounded_arun(self, crawler, url: str, cfg, timeout: float, *,
                            recycle_stealth: bool = False):
        """``crawler.arun`` with an outer stdlib deadline so a wedged Chromium/CDP pipe
        cannot hold a semaphore permit forever. Crawl4AI's page_timeout stays the primary
        mechanism; the +_RENDER_GRACE_S outer bound fires only when it already failed to.

        ``recycle_stealth`` recycles the wedged stealth crawler on timeout so one wedge
        no longer poisons the anti-bot tier until process restart (self-healing)."""
        try:
            async with asyncio.timeout(timeout + _RENDER_GRACE_S):
                return await crawler.arun(url, config=cfg)
        except TimeoutError as e:
            if recycle_stealth:
                await self._recycle_stealth()
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
            # Default True re-enables crawl4ai's console logger per run (BrowserConfig's
            # verbose=False alone does not stick), flooding the log with per-URL lines and
            # source dumps. Failures still surface through argus.fetch.
            verbose=False,
        )

        crawler = await self._ensure_stealth() if stealth else self._crawler
        tier = "stealth" if stealth else "normal"
        async with self._sem:
            res = await self._bounded_arun(crawler, url, cfg, timeout, recycle_stealth=stealth)
            # Page errors say "closed" too (window.close, a crashed tab); only relaunch
            # when the browser process itself is confirmed gone.
            if _browser_dead(res) and not self.alive(crawler):
                if stealth:
                    await self._recycle_stealth()  # next stealth call re-inits it
                else:
                    try:
                        await self._restart_normal(crawler)
                        res = await self._bounded_arun(self._crawler, url, cfg, timeout)
                    except FetchError:
                        raise
                    except Exception as e:  # noqa: BLE001 - relaunch failed; surface as render
                        raise FetchError("render_failed", "browser relaunch failed") from e

        # Auto-escalate to the stealth tier once on an anti-bot block.
        blocked = not res.success or _looks_blocked(
            getattr(res, "html", ""), getattr(res, "status_code", None)
        )
        if not stealth and blocked:
            stealth_crawler = await self._ensure_stealth()
            async with self._sem:
                res2 = await self._bounded_arun(
                    stealth_crawler, url, cfg, timeout, recycle_stealth=True
                )
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
            # res.url is the URL we asked for; redirected_url is where the page landed.
            "final_url": getattr(res, "redirected_url", None) or res.url or url,
            "html": res.html,
            "screenshot": res.screenshot if screenshot else None,
            "render_tier": tier,
        }
