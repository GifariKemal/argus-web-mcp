"""Deep-crawl tier - Crawl4AI BFS multi-page crawl behind the SSRF boundary.

We validate the *seed* URL (scheme allowlist + resolve-then-validate the host),
then hand the crawl to Crawl4AI's BFSDeepCrawlStrategy. With ``same_domain=True``
a DomainFilter pins discovery to the seed host, which is what confines the crawl
to a known-safe origin.

SSRF ceiling (P2): unlike the httpx tier, discovered URLs are NOT individually
resolve-then-validated/IP-pinned - Crawl4AI drives Chromium, which does its own
DNS, and there is no per-URL re-pin hook. The seed-host validation plus the
same-domain DomainFilter are the trust boundary: the crawl cannot wander to an
arbitrary domain (and thus not to a metadata/private IP) because every followed
link must match the validated seed host. Cross-domain crawls (same_domain=False)
relax this and should only be used for hosts already trusted by the caller.
ponytail: a per-URL re-pin would require a custom dispatcher/proxy in Crawl4AI;
defer until a concrete need appears.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..security.ssrf import aresolve_and_validate, validate_url
from .static import _DEFAULT_PORTS


def _build_config(
    seed_url: str,
    *,
    depth: int,
    max_pages: int,
    include: list[str] | None,
    exclude: list[str] | None,
    same_domain: bool,
    respect_robots: bool,
    timeout: float = 45,
):
    """Pure builder for the deep-crawl CrawlerRunConfig (no network/browser)."""
    from crawl4ai import (
        BFSDeepCrawlStrategy,
        CacheMode,
        CrawlerRunConfig,
        DomainFilter,
        FilterChain,
        URLPatternFilter,
    )

    filters: list = []
    if same_domain:
        host = urlsplit(seed_url).hostname
        if host:
            filters.append(DomainFilter(allowed_domains=[host]))
    if include:
        filters.append(URLPatternFilter(patterns=list(include)))
    if exclude:
        filters.append(URLPatternFilter(patterns=list(exclude), reverse=True))

    strategy = BFSDeepCrawlStrategy(
        max_depth=depth,
        max_pages=max_pages,
        filter_chain=FilterChain(filters),
    )
    return CrawlerRunConfig(
        deep_crawl_strategy=strategy,
        check_robots_txt=respect_robots,
        cache_mode=CacheMode.BYPASS,
        stream=False,
        page_timeout=int(timeout * 1000),  # per-page bound (ms), matches the render tier
    )


def _markdown(result) -> str:
    """Best-effort markdown text from a CrawlResult (md is an object or str)."""
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    raw = getattr(md, "raw_markdown", None)
    return raw if raw is not None else str(md)


def _child_urls(result) -> list[str]:
    """Extract internal child link hrefs from a CrawlResult.links dict."""
    links = getattr(result, "links", None) or {}
    internal = links.get("internal", []) if isinstance(links, dict) else []
    out: list[str] = []
    for link in internal:
        href = link.get("href") if isinstance(link, dict) else getattr(link, "href", None)
        if href:
            out.append(href)
    return out


def _shape(results) -> dict:
    """Shape a list of CrawlResult into {pages, link_graph, count}. Drops failures."""
    pages: list[dict] = []
    link_graph: dict[str, list[str]] = {}
    for res in results:
        if not getattr(res, "success", False):
            continue
        meta = getattr(res, "metadata", None) or {}
        pages.append(
            {
                "url": res.url,
                "title": meta.get("title"),
                "content": _markdown(res),
                "depth": meta.get("depth", 0),
            }
        )
        link_graph[res.url] = _child_urls(res)
    return {"pages": pages, "link_graph": link_graph, "count": len(pages)}


async def _run(crawler, seed_url: str, cfg) -> list:
    """Run the crawl; arun returns a list (stream=False) or an async generator."""
    res = await crawler.arun(seed_url, config=cfg)
    if hasattr(res, "__aiter__"):
        return [r async for r in res]
    if isinstance(res, list):
        return res
    return [res]


async def deep_crawl(
    seed_url: str,
    *,
    depth: int = 2,
    max_pages: int = 50,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    same_domain: bool = True,
    respect_robots: bool = True,
    browser=None,
    timeout: float = 45,
) -> dict:
    """BFS deep-crawl from ``seed_url``. Returns {pages, link_graph, count}.

    Validates the seed (scheme + resolve-then-validate the host), confines the
    crawl to the seed host when ``same_domain`` (DomainFilter), applies
    include/exclude glob filters, and shapes per-page results. Partial page
    failures are skipped, not fatal. ``timeout`` bounds each PAGE load (Crawl4AI
    page_timeout); the whole-crawl wall clock is bounded by the server tool
    (TIMEOUTS['crawl'] / ARGUS_TIMEOUT_CRAWL). See module docstring for the SSRF
    ceiling.
    """
    validate_url(seed_url)
    parts = urlsplit(seed_url)
    await aresolve_and_validate(parts.hostname, parts.port or _DEFAULT_PORTS[parts.scheme])

    cfg = _build_config(
        seed_url,
        depth=depth,
        max_pages=max_pages,
        include=include,
        exclude=exclude,
        same_domain=same_domain,
        respect_robots=respect_robots,
        timeout=timeout,
    )

    crawler = getattr(browser, "_crawler", None)
    if crawler is not None:
        # Hold ONE BrowserPool permit for the crawl's duration so its page loads are
        # accounted by the same RAM guard as every other browser-tier tool (a 50-page
        # BFS must not run invisibly beside 4 concurrent scrapes on the shared Chromium).
        sem = getattr(browser, "_sem", None)
        if sem is not None:
            async with sem:
                results = await _run(crawler, seed_url, cfg)
        else:
            results = await _run(crawler, seed_url, cfg)
    else:
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        await crawler.start()
        try:
            results = await _run(crawler, seed_url, cfg)
        finally:
            await crawler.close()

    return _shape(results)
