"""URL/sitemap discovery (the ``map`` tool).

Discover a site's URLs cheaply, without a full crawl. Strategy, in order:

1. **robots.txt** - fetch ``<origin>/robots.txt`` and parse ``Sitemap:`` directives.
2. **sitemap** - fetch the discovered sitemaps (or ``<origin>/sitemap.xml`` if robots
   named none); parse ``<loc>`` entries. A ``<sitemapindex>`` is expanded by fetching a
   capped number of child sitemaps and merging their ``<loc>`` entries.
3. **links** - fall back to fetching the page HTML and extracting same-site ``<a href>``
   links (1 hop).

Discovered URLs are deduped, scoped to the seed's registrable domain (or exact host when
``include_subdomains=False``), each re-checked through the SSRF guard (``validate_url``),
and capped at ``max_urls``. The seed itself is resolve-then-validated up front so an SSRF
block on the seed propagates as ``SSRFError`` before any discovery work happens.
"""

from __future__ import annotations

import gzip
import io
import re
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx

from .fetch.static import (
    _DEFAULT_PORTS,
    MAX_FETCH_BYTES,
    FetchError,
    _get_guarded,
    fetch_static,
)
from .security.ssrf import (
    SSRFError,
    build_safe_async_client,
    aresolve_and_validate,
    validate_url,
)

# Cap on child sitemaps fetched from a <sitemapindex> to bound work on the shared box.
_MAX_CHILD_SITEMAPS = 10
# Two-label public suffixes where the registrable domain is the last *three* labels
# (e.g. example.co.uk). Heuristic - no full PSL dependency (ponytail: stdlib only).
_TWO_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "co.jp", "or.jp", "ne.jp", "com.au",
    "net.au", "org.au", "co.nz", "co.za", "com.br", "com.cn", "com.sg",
    # User-namespace public suffixes: each user/site gets its own subdomain, so the
    # registrable domain includes that label (user.github.io != other.github.io).
    "github.io", "gitlab.io", "netlify.app", "vercel.app", "pages.dev",
}
_HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#\s][^"']*)["']""", re.IGNORECASE)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 (no PSL): last 2 labels, or 3 for known two-label suffixes."""
    host = (host or "").lower().rstrip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _in_scope(url: str, seed_host: str, *, include_subdomains: bool) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if include_subdomains:
        return _registrable_domain(host) == _registrable_domain(seed_host)
    return host == seed_host.lower()


def _local_name(tag: str) -> str:
    """Strip the XML namespace from a tag: ``{ns}loc`` -> ``loc``."""
    return tag.rsplit("}", 1)[-1].lower()


def _parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Return (loc_urls, child_sitemap_urls). Raises on malformed XML."""
    root = ET.fromstring(xml)  # noqa: S314 - HTML/XML from guarded fetch, no entity use
    is_index = _local_name(root.tag) == "sitemapindex"
    locs: list[str] = []
    for loc in root.iter():
        if _local_name(loc.tag) == "loc" and loc.text:
            locs.append(loc.text.strip())
    return ([], locs) if is_index else (locs, [])


async def _get(url: str, *, client: httpx.AsyncClient, timeout: int) -> str | None:
    """Fetch text, returning None on any non-200 / fetch failure (SSRFError propagates)."""
    try:
        res = await fetch_static(url, client=client, timeout=timeout)
    except FetchError:
        return None
    return res["html"] if res["status"] == 200 else None


def _robots_sitemaps(robots: str, base: str) -> list[str]:
    out: list[str] = []
    for line in robots.splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip().lower() == "sitemap" and val.strip():
            out.append(urljoin(base, val.strip()))
    return out


async def _get_sitemap_xml(url: str, *, client: httpx.AsyncClient, timeout: int) -> str | None:
    """Fetch a sitemap as XML text, transparently un-gzipping ``.xml.gz`` payloads.

    Sniffs the gzip magic (not the URL suffix): httpx already un-gzips
    Content-Encoding responses, so a ``.gz`` URL may arrive as plain XML, and a plain
    URL may serve a raw gzip file. The DECOMPRESSED size is hard-capped at
    ``MAX_FETCH_BYTES`` (zip-bomb guard). Returns None on any fetch/decompress failure.
    """
    try:
        resp, data = await _get_guarded(url, client=client, timeout=timeout, max_redirects=5)
    except FetchError:
        return None
    if resp.status_code != 200:
        return None
    if data[:2] == b"\x1f\x8b":
        try:
            raw = gzip.GzipFile(fileobj=io.BytesIO(data)).read(MAX_FETCH_BYTES + 1)
        except (OSError, EOFError):
            return None
        if len(raw) > MAX_FETCH_BYTES:
            return None
        data = raw
    return data.decode("utf-8", "replace")


async def _collect_from_sitemaps(
    sitemap_urls: list[str], *, client: httpx.AsyncClient, timeout: int
) -> list[str]:
    """Fetch+parse sitemaps, expanding one level of <sitemapindex> (capped)."""
    locs: list[str] = []
    pending = list(sitemap_urls)
    children_fetched = 0
    while pending:
        sm = pending.pop(0)
        xml = await _get_sitemap_xml(sm, client=client, timeout=timeout)
        if xml is None:
            continue
        try:
            page_locs, child_sitemaps = _parse_sitemap(xml)
        except ET.ParseError:
            continue
        locs.extend(page_locs)
        for child in child_sitemaps:
            if children_fetched >= _MAX_CHILD_SITEMAPS:
                break
            children_fetched += 1
            pending.append(child)
    return locs


def _extract_links(html: str, base: str) -> list[str]:
    return [urljoin(base, m) for m in _HREF_RE.findall(html)]


def _finalize(
    candidates: list[str], *, seed_host: str, include_subdomains: bool, max_urls: int
) -> tuple[list[str], bool]:
    """Dedup (order-preserving), scope-filter, SSRF-check, cap. Returns (urls, truncated)."""
    seen: set[str] = set()
    kept: list[str] = []
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if not _in_scope(url, seed_host, include_subdomains=include_subdomains):
            continue
        try:
            validate_url(url)
        except SSRFError:  # noqa: S112 - a bad URL is skipped, not logged per-item
            continue
        kept.append(url)
    truncated = len(kept) > max_urls
    return kept[:max_urls], truncated


class MapError(Exception):
    """Raised when no URLs could be discovered by any strategy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def map_site(
    url: str,
    *,
    max_urls: int = 500,
    include_subdomains: bool = True,
    client: httpx.AsyncClient | None = None,
    timeout: int = 20,
) -> dict:
    """Discover URLs for the site at ``url``. See module docstring for the strategy order.

    Returns ``{url, urls, count, source, truncated}`` where ``source`` is one of
    ``'robots+sitemap'``, ``'sitemap'`` or ``'links'``. Raises ``SSRFError`` if the seed is
    blocked, or ``MapError('fetch_failed')`` if nothing could be discovered.
    """
    validate_url(url)
    parts = urlsplit(url)
    seed_host = parts.hostname or ""
    # Re-pin the seed up front so an SSRF block surfaces before any discovery work.
    await aresolve_and_validate(seed_host, parts.port or _DEFAULT_PORTS[parts.scheme])

    origin = _origin(url)

    def shaped(urls: list[str], source: str, truncated: bool) -> dict:
        return {
            "url": url,
            "urls": urls,
            "count": len(urls),
            "source": source,
            "truncated": truncated,
        }

    async def _discover(c: httpx.AsyncClient) -> dict:
        # 1. robots.txt -> sitemap directives.
        robots = await _get(urljoin(origin, "/robots.txt"), client=c, timeout=timeout)
        robots_sitemaps = _robots_sitemaps(robots, origin) if robots else []

        # 2. sitemaps (from robots, else the conventional /sitemap.xml).
        sitemap_urls = robots_sitemaps or [urljoin(origin, "/sitemap.xml")]
        locs = await _collect_from_sitemaps(sitemap_urls, client=c, timeout=timeout)
        if locs:
            urls, truncated = _finalize(
                locs, seed_host=seed_host, include_subdomains=include_subdomains,
                max_urls=max_urls,
            )
            if urls:
                source = "robots+sitemap" if robots_sitemaps else "sitemap"
                return shaped(urls, source, truncated)

        # 3. fallback: same-site links from the page HTML (1 hop).
        html = await _get(url, client=c, timeout=timeout)
        if html:
            urls, truncated = _finalize(
                _extract_links(html, url),
                seed_host=seed_host,
                include_subdomains=include_subdomains,
                max_urls=max_urls,
            )
            if urls:
                return shaped(urls, "links", truncated)

        raise MapError("fetch_failed", f"no URLs discoverable for {url!r}")

    # A direct call with no client gets a default SSRF-guarded one, closed when we're done.
    if client is None:
        own = build_safe_async_client(timeout=timeout)
        try:
            return await _discover(own)
        finally:
            await own.aclose()
    return await _discover(client)
