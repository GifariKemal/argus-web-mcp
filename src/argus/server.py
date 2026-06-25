"""Argus FastMCP server - 20 web tools over a tiered, SSRF-guarded, cached fetch core.

Run locally (P1) over stdio:  python -m argus.server
Tools NEVER raise to the client - they return structured ``err(...)`` dicts.

State (httpx safe client, cache, browser pool) is created in the lifespan and held in a
module-level ``_S``. Tests set ``server._S`` directly and call the tool functions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse

from . import semantic
from .cache import Cache, ttl_for
from .config import HEALTH_LATENCY_BUCKETS, TIMEOUTS
from .extract.article import extract_article
from .extract.links import extract_links_images
from .extract.llm import extract_llm, llm_available
from .extract.pdf import extract_pdf, extract_pdf_quality
from .extract.structured import extract_selectors
from .fetch.core import fetch
from .fetch.crawl import CrawlError, deep_crawl
from .fetch.render import BrowserPool
from .fetch.static import FetchError, fetch_bytes
from .gh_search import GitHubSearchError
from .gh_search import github_search as _gh_search
from .mapsite import MapError, map_site
from .models import err
from .research import research as _research
from .router import classify
from .scholar import ScholarError
from .scholar import scholar_search as _scholar_search
from .search import SearchError
from .search import search as searxng_search
from .security.ssrf import SSRFError, build_safe_async_client, validate_url
from .trading.cot import cot_report as _cot_report
from .trading.forexfactory import forexfactory_calendar as _ff_calendar
from .trading.news import news_sentiment_feed as _news_feed
from .watch import WatchStore, poll_due

INSTRUCTIONS = (
    "Argus: self-hosted web tools. `read(url)` clean article markdown; `search(query)` web "
    "search (SearXNG); `read_pdf(url, mode)` PDF->markdown+tables (mode='quality' for "
    "scanned/complex via Docling); `scrape(url, screenshot)` JS-rendered pages w/ anti-bot "
    "auto-escalation; `batch_read(urls)` many URLs in parallel (partial-failure tolerant); "
    "`crawl(seed, depth)` site deep-crawl (robots-respecting); `screenshot(url)` full-page PNG; "
    "`research(query, mode)` one-shot research: deep (full-read bundle) / quick (hits) / answer "
    "(cited LLM answer); `map_urls(url)` discover a site's URLs (sitemap/robots/links); "
    "`find_similar(url/text)` semantically-related pages (local embeddings); "
    "`github_search(query, mode)` structured GitHub repos/code/issues; `scholar_search(query)` "
    "academic papers (Semantic Scholar/CrossRef: citations/DOI/abstract); `smart_search(query)` "
    "auto-routes to the best backend (github/scholar/news/it/general); "
    "`extract_structured(url, schema, mode)` pull fields via CSS/XPath, mode='llm'/'auto' "
    "uses an LLM. `watch(url, webhook)`/`list_watches`/`unwatch` monitor a page -> webhook on "
    "change. Trading: `forexfactory_calendar`, `cot_report`, `news_sentiment_feed`. "
    "All fetches SSRF-guarded + cached; full content (no silent truncation). "
    "Errors come back as {error, code, detail}."
)

BATCH_CAP = 200
MAX_PDF_BYTES = 64 * 1024 * 1024

logger = logging.getLogger("argus.server")

# Per-tool latency samples (deque maxlen for bounded memory). Filled by _MetricsMiddleware.
_tool_latencies: dict[str, deque[float]] = {}


_STARTUP_TIME: float | None = None


def _latency_percentiles(name: str) -> dict:
    """Return p50/p90/p99 for a tool, or empty dict if no samples."""
    samples = _tool_latencies.get(name)
    if not samples:
        return {}
    s = sorted(samples)
    n = len(s)
    def _p(k: float) -> float:
        idx = int(n * k)
        idx = min(idx, n - 1)
        return round(s[max(0, idx)], 3)
    return {
        "p50": _p(0.50),
        "p90": _p(0.90),
        "p99": _p(0.99),
        "count": n,
        "min": round(s[0], 3),
        "max": round(s[-1], 3),
    }


def _safe_detail(exc: Exception) -> str:
    """Sanitized error detail for the client. The full exception (which may carry internal
    URLs/paths/keys) is LOGGED; the client only gets the exception class name, never its
    message text. Keeps err() detail non-empty without leaking internals (Sec-F2)."""
    logger.warning("tool error: %s: %s", type(exc).__name__, exc)
    return type(exc).__name__


@dataclass
class State:
    client: object
    cache: Cache
    browser: BrowserPool | None
    throttle: object | None = None
    watch_store: object | None = None


_S: State | None = None


def _state() -> State:
    if _S is None:  # pragma: no cover - lifespan guarantees this in production
        raise RuntimeError("Argus state not initialised (no lifespan running)")
    return _S


@asynccontextmanager
async def lifespan(_server: FastMCP):
    global _S, _STARTUP_TIME
    from .fetch.throttle import HostThrottle

    _STARTUP_TIME = time.monotonic()
    courtesy = float(os.environ.get("ARGUS_COURTESY_DELAY", "1.0"))
    max_ctx = int(os.environ.get("ARGUS_MAX_CONCURRENT_CONTEXTS", "4"))
    # httpx client timeout: use the largest configured tool timeout so no tool is capped
    # by the underlying client before its own timeout fires.
    client_timeout = max(TIMEOUTS.values(), default=60) + 15
    _S = State(
        client=build_safe_async_client(timeout=client_timeout),
        cache=Cache(),
        browser=BrowserPool(concurrency=max_ctx),
        throttle=HostThrottle(min_interval=courtesy),
        watch_store=WatchStore(),
    )
    await _S.browser.start()
    # Pre-warm the local embedding model off the event loop so the FIRST research/find_similar
    # doesn't eat the one-time ~5s HF model load. Best-effort: never blocks/fails startup
    # (no-op when the [semantic] extra is absent).
    warm_task = asyncio.create_task(asyncio.to_thread(semantic.warm))
    watch_task = asyncio.create_task(_watch_loop())
    try:
        yield
    finally:
        watch_task.cancel()
        warm_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch_task
        with contextlib.suppress(asyncio.CancelledError):
            await warm_task
        if _S.browser is not None:
            await _S.browser.stop()
        await _S.client.aclose()
        _S.cache.close()
        _S = None


WATCH_TICK_S = 60


async def _watch_loop() -> None:
    """Background poller: every WATCH_TICK_S, check due watches -> deliver on change."""
    while True:
        await asyncio.sleep(WATCH_TICK_S)
        s = _S
        if s is None or s.watch_store is None:
            continue

        async def _fetch_fn(url: str, _s=s) -> dict:
            # raw html so selector watches parse correctly; full-content watches hash the html.
            return await fetch(url, client=_s.client, browser=_s.browser, throttle=_s.throttle)

        try:
            await poll_due(s.watch_store, fetch_fn=_fetch_fn, client=s.client, now=time.time())
        except Exception:  # noqa: BLE001,S110 - poller must never die; next tick retries
            pass


def _is_url(s: str) -> bool:
    return urlsplit(s).scheme in {"http", "https"}


def _read_local_pdf(path: str) -> bytes | None:
    """Blocking local-file read, run in a thread by read_pdf. None if missing."""
    p = Path(path).expanduser()
    return p.read_bytes() if p.is_file() else None


# --- tools -------------------------------------------------------------------


async def read(
    url: str,
    format: str = "markdown",
    clean: bool = True,
    include_links: bool = False,
    extract_media: bool = False,
    timeout: int = TIMEOUTS["read"],
) -> dict:
    """Fetch a URL -> clean main content (no truncation). `extract_media`=True also returns the
    page's links + images lists."""
    s = _state()
    try:
        validate_url(url)
    except SSRFError as e:
        return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))

    opts = {"format": format, "clean": clean, "include_links": include_links,
            "media": extract_media}
    ck = s.cache.key(url, opts)
    cached = s.cache.get(ck, ttl_for("general"))
    if cached is not None:
        return {**cached, "from_cache": True}

    try:
        res = await fetch(url, client=s.client, browser=s.browser, timeout=timeout,
                          throttle=s.throttle)
    except SSRFError as e:
        return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))
    except FetchError as e:
        stale = s.cache.get_stale(ck)
        if stale is not None:
            return {**stale, "from_cache": True}
        return err("fetch_failed", "fetch failed", _safe_detail(e))

    art = extract_article(res["html"], res["final_url"], fmt=format, clean=clean,
                          include_links=include_links)
    if not art["content"]:
        return err("empty_content", "no extractable content", res["final_url"])

    out = {
        "url": url,
        "final_url": res["final_url"],
        "status": res["status"],
        "title": art["title"],
        "content": art["content"],
        "format": format,
        "metadata": art["metadata"],
        "render_path": res["render_path"],
        "from_cache": False,
    }
    if extract_media:
        media = extract_links_images(res["html"], res["final_url"])
        out["links"] = media["links"]
        out["images"] = media["images"]
    s.cache.put(ck, out, source="general")
    return out


async def search(
    query: str | list[str],
    count: int = 10,
    category: str = "general",
    time_range: str | None = None,
    lang: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    safesearch: int = 0,
) -> dict:
    """Web search via self-hosted SearXNG (unlimited). Optional domain allow/deny + safesearch."""
    s = _state()
    qkey = query if isinstance(query, str) else " ".join(query)
    ck = s.cache.key(
        "search:" + qkey,
        {"count": count, "category": category, "time_range": time_range, "lang": lang,
         "inc": include_domains, "exc": exclude_domains, "safe": safesearch},
    )
    cached = s.cache.get(ck, ttl_for("search"))
    if cached is not None:
        return {**cached, "from_cache": True}

    try:
        res = await searxng_search(
            query, count=count, category=category, time_range=time_range, lang=lang,
            include_domains=include_domains, exclude_domains=exclude_domains, safesearch=safesearch,
        )
    except SearchError as e:
        if e.code == "no_results":
            return err("no_results", "no search results", qkey)
        return err("search_backend_down", "search backend unavailable", _safe_detail(e))

    res["from_cache"] = False
    s.cache.put(ck, res, source="search")
    return res


async def read_pdf(
    url_or_path: str,
    pages: str | None = None,
    mode: str = "text",
    timeout: int = TIMEOUTS["read_pdf"],
) -> dict:
    """PDF (URL or local path) -> markdown + tables."""
    s = _state()
    try:
        if _is_url(url_or_path):
            validate_url(url_or_path)
            _, data, _ctype = await fetch_bytes(url_or_path, client=s.client, timeout=timeout)
        else:
            # SECURITY: local-path read is an LFI primitive on a remote server. Secure-by-default:
            # denied unless ARGUS_ALLOW_LOCAL_PDF=1 (set only for local dev / a sandboxed deploy).
            if os.environ.get("ARGUS_ALLOW_LOCAL_PDF") != "1":
                return err("fetch_failed", "local-path PDF reads are disabled on this server")
            data = await asyncio.to_thread(_read_local_pdf, url_or_path)
            if data is None:
                return err("fetch_failed", "file not found", url_or_path)
    except SSRFError as e:
        return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))
    except FetchError as e:
        return err("fetch_failed", "fetch failed", _safe_detail(e))

    if len(data) > MAX_PDF_BYTES:
        return err("parse_failed", "PDF too large", f"{len(data)} bytes")

    try:
        if mode == "quality":
            result = await asyncio.to_thread(extract_pdf_quality, data, pages)  # Docling (heavy)
        else:
            result = extract_pdf(data, pages, mode)
    except ValueError:
        return err("not_pdf", "not a valid PDF", url_or_path)
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("parse_failed", "PDF parse failed", _safe_detail(e))

    return {"source": url_or_path, **result}


async def scrape(
    url: str,
    wait_for: str | None = None,
    actions: list | None = None,
    screenshot: bool = False,
    format: str = "markdown",
    timeout: int = TIMEOUTS["scrape"],
) -> dict:
    """JS-rendered fetch (+ optional screenshot/interactions) via the browser tier."""
    s = _state()
    try:
        validate_url(url)
    except SSRFError as e:
        return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))
    if s.browser is None:
        return err("render_failed", "browser tier unavailable")

    try:
        res = await fetch(
            url, render=True, wait_for=wait_for, actions=actions, screenshot=screenshot,
            timeout=timeout, browser=s.browser, client=s.client, throttle=s.throttle,
        )
    except SSRFError as e:
        return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))
    except FetchError as e:
        code = "blocked_by_antibot" if "antibot" in str(e).lower() else "render_failed"
        return err(code, "render failed", _safe_detail(e))

    art = extract_article(res["html"], res["final_url"], fmt=format)
    return {
        "url": url,
        "final_url": res["final_url"],
        "content": art["content"] or res["html"],
        "format": format,
        "screenshot": res.get("screenshot"),
        "render_path": "browser",
    }


async def batch_read(
    urls: list[str], concurrency: int = 8, format: str = "markdown", clean: bool = True
) -> dict:
    """Parallel `read` over many URLs - partial-failure tolerant."""
    note = None
    if len(urls) > BATCH_CAP:
        note = f"capped to first {BATCH_CAP} of {len(urls)} urls"
        urls = urls[:BATCH_CAP]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(u: str) -> dict:
        async with sem:
            r = await read(u, format=format, clean=clean)
        if isinstance(r, dict) and r.get("code") in {
            "ssrf_blocked", "fetch_failed", "empty_content",
        }:
            return {"url": u, "ok": False, "error": r}
        return {"url": u, "ok": True, "content": r["content"], "title": r.get("title")}

    results = await asyncio.gather(*(one(u) for u in urls))
    succeeded = sum(1 for r in results if r["ok"])
    out = {"results": results, "succeeded": succeeded, "failed": len(results) - succeeded}
    if note:
        out["note"] = note
    return out


async def extract_structured(
    url_or_urls: str | list[str], schema: dict, prompt: str | None = None, mode: str = "auto"
) -> dict:
    """URL(s) -> schema-validated JSON.

    mode='selector' (schema = field->CSS/XPath), 'llm' (schema = field->type, needs an LLM key),
    'auto' (selector first; LLM fallback when selectors come back invalid and an LLM is available).
    """
    s = _state()
    if not isinstance(schema, dict) or not schema:
        return err("schema_invalid", "schema must be a non-empty field map")
    if mode not in {"selector", "llm", "auto"}:
        return err("schema_invalid", f"unknown mode {mode!r}")
    if mode == "llm" and not llm_available():
        return err("extraction_failed", "LLM mode needs ARGUS_LLM_API_KEY/OPENAI_API_KEY")

    single = isinstance(url_or_urls, str)
    urls = [url_or_urls] if single else list(url_or_urls)
    out = []
    for u in urls:
        try:
            validate_url(u)
            # selector tier runs on the fetched HTML - no thin-content browser escalation
            # (escalation would replace the page with rendered content and lose the targets).
            res = await fetch(u, client=s.client, browser=None, throttle=s.throttle)
        except SSRFError as e:
            out.append(
                {"url": u, **err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))}
            )
            continue
        except FetchError as e:
            out.append({"url": u, **err("fetch_failed", "fetch failed", _safe_detail(e))})
            continue

        result = None
        if mode in {"selector", "auto"}:
            try:
                r = extract_selectors(res["html"], schema)
                result = {"url": u, "data": r["data"], "valid": r["valid"], "mode_used": "selector"}
            except Exception as e:  # noqa: BLE001 - never raise to client
                if mode == "selector":
                    out.append(
                        {"url": u, **err("extraction_failed", "selector failed", _safe_detail(e))}
                    )
                    continue

        need_llm = mode == "llm" or (
            mode == "auto" and (result is None or not result["valid"]) and llm_available()
        )
        if need_llm:
            art = extract_article(res["html"], res["final_url"])
            content = art["content"] or res["html"]
            llm_schema = schema if mode == "llm" else dict.fromkeys(schema, "str")
            try:
                lr = await extract_llm(content, llm_schema, prompt=prompt)
                result = {"url": u, "data": lr["data"], "valid": lr["valid"], "mode_used": "llm"}
            except Exception as e:  # noqa: BLE001 - LLMUnavailable/parse errors; never raise to client
                if result is None:
                    out.append(
                        {"url": u, **err("extraction_failed", "LLM failed", _safe_detail(e))}
                    )
                    continue
        out.append(result)

    return out[0] if single else {"results": out}


async def crawl(
    seed_url: str, depth: int = 2, max_pages: int = 50, include: list | None = None,
    exclude: list | None = None, same_domain: bool = True, respect_robots: bool = True,
) -> dict:
    """Deep-crawl a site (robots-respecting, confined to the seed host by default)."""
    s = _state()
    if s.browser is None:
        return err("render_failed", "browser tier unavailable")
    try:
        return await deep_crawl(
            seed_url, depth=depth, max_pages=max_pages, include=include, exclude=exclude,
            same_domain=same_domain, respect_robots=respect_robots, browser=s.browser,
        )
    except SSRFError as e:
        return err("ssrf_blocked", "seed URL blocked by SSRF guard", _safe_detail(e))
    except CrawlError as e:
        return err("fetch_failed", "crawl failed", _safe_detail(e))


async def screenshot(url: str, timeout: int = TIMEOUTS["screenshot"]) -> dict:
    """Full-page PNG screenshot (base64) of a JS-rendered page."""
    s = _state()
    try:
        validate_url(url)
    except SSRFError as e:
        return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))
    if s.browser is None:
        return err("render_failed", "browser tier unavailable")
    try:
        res = await fetch(
            url, render=True, screenshot=True, timeout=timeout,
            browser=s.browser, client=s.client, throttle=s.throttle,
        )
    except SSRFError as e:
        return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))
    except FetchError as e:
        code = "blocked_by_antibot" if "antibot" in str(e).lower() else "render_failed"
        return err(code, "screenshot failed", _safe_detail(e))
    return {"url": url, "final_url": res["final_url"], "screenshot": res.get("screenshot"),
            "format": "png"}


async def forexfactory_calendar(date_range: list | None = None) -> dict:
    """ForexFactory economic calendar (FairEconomy JSON feed) in Aurix calendar shape."""
    s = _state()
    try:
        return await _ff_calendar(date_range, client=s.client)
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("fetch_failed", "forexfactory calendar failed", _safe_detail(e))


async def cot_report(report_type: str = "legacy_futures", date: str | None = None) -> dict:
    """CFTC Commitments of Traders positioning."""
    s = _state()
    try:
        return await _cot_report(report_type=report_type, date=date, client=s.client)
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("fetch_failed", "COT report failed", _safe_detail(e))


async def news_sentiment_feed(
    query: str, since: str | None = None, sentiment: bool = False
) -> dict:
    """Ranked news feed (+ optional owned-LLM sentiment score)."""
    s = _state()
    try:
        return await _news_feed(query, since=since, sentiment=sentiment, client=s.client)
    except SearchError as e:
        code = "no_results" if e.code == "no_results" else "search_backend_down"
        return err(code, "news feed failed", _safe_detail(e))
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("extraction_failed", "news feed failed", _safe_detail(e))


def _build_auth():
    """Bearer auth from env. Precedence: JWT (prod, rotation/expiry) -> static token -> none.

    - ARGUS_JWT_JWKS_URI (+ optional ARGUS_JWT_ISSUER/ARGUS_JWT_AUDIENCE) -> JWTVerifier.
    - else ARGUS_TOKEN -> StaticTokenVerifier (dev/internal).
    - else None -> no auth (local stdio dev / tests).
    """
    jwks = os.environ.get("ARGUS_JWT_JWKS_URI")
    if jwks:
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        return JWTVerifier(
            jwks_uri=jwks,
            issuer=os.environ.get("ARGUS_JWT_ISSUER"),
            audience=os.environ.get("ARGUS_JWT_AUDIENCE"),
        )
    token = os.environ.get("ARGUS_TOKEN")
    if not token:
        return None
    from fastmcp.server.auth import StaticTokenVerifier

    return StaticTokenVerifier(
        tokens={token: {"client_id": "argus", "scopes": ["use"]}},
        required_scopes=["use"],
    )


async def research(
    query: str, mode: str = "deep", max_sources: int = 5, highlights: bool = False,
    max_chars_per_source: int | None = None, timeout: int = TIMEOUTS["research"],
) -> dict:
    """Deep research in one call. mode='deep' (default) = search + parallel FULL read of the top
    sources -> consolidated complete content (replaces search->fetch->repeat). mode='quick' = ranked
    hits (title/url/snippet) only, zero fetches (fast). `highlights`=True attaches the top
    query-relevant sentences per source (local embeddings; deep mode). `max_chars_per_source`
    (opt-in) caps each source's content for token-sensitive callers; truncation is FLAGGED
    (truncated=True + full_chars), word_count preserved - default None returns FULL content."""
    s = _state()
    ck = s.cache.key("research:" + query,
                     {"mode": mode, "max_sources": max_sources, "hl": highlights,
                      "mcps": max_chars_per_source})
    cached = s.cache.get(ck, ttl_for("general"))
    if cached is not None:
        return {**cached, "from_cache": True}

    try:
        res = await _research(
            query, mode=mode, max_sources=max_sources,
            max_chars_per_source=max_chars_per_source, timeout=timeout,
            client=s.client, browser=s.browser,
        )
    except ValueError as e:  # invalid mode
        return err("schema_invalid", "invalid research mode", _safe_detail(e))
    except SearchError as e:
        code = "no_results" if e.code == "no_results" else "search_backend_down"
        return err(code, "research search failed", _safe_detail(e))
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("extraction_failed", "research failed", _safe_detail(e))

    # Optional per-source highlights (top query-relevant sentences) - deep bundle + semantic on.
    if highlights and isinstance(res, dict) and semantic.available():
        for src in res.get("sources", []):
            content = src.get("content")
            if content:
                src["highlights"] = semantic.top_sentences(query, content, top_k=3)

    # Only cache success - never an error dict (e.g. mode='answer' LLM failure).
    if isinstance(res, dict) and "code" not in res:
        res["from_cache"] = False
        s.cache.put(ck, res, source="general")
    return res


async def map_urls(url: str, max_urls: int = 500, include_subdomains: bool = True) -> dict:
    """Discover a site's URLs via sitemap.xml / robots.txt / 1-hop links (no full fetch)."""
    s = _state()
    ck = s.cache.key("map:" + url, {"max_urls": max_urls, "include_subdomains": include_subdomains})
    cached = s.cache.get(ck, ttl_for("docs"))
    if cached is not None:
        return {**cached, "from_cache": True}

    try:
        res = await map_site(url, max_urls=max_urls, include_subdomains=include_subdomains,
                             client=s.client)
    except SSRFError as e:
        return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))
    except MapError as e:
        return err("fetch_failed", "site map failed", _safe_detail(e))
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("fetch_failed", "map failed", _safe_detail(e))

    if isinstance(res, dict) and "code" not in res:
        res["from_cache"] = False
        s.cache.put(ck, res, source="docs")
    return res


async def find_similar(url_or_text: str, count: int = 10) -> dict:
    """Find pages semantically similar to a URL's content or a text snippet (local embeddings,
    no API). Needs the [semantic] extra (fastembed). Argus's Exa-`findSimilar` equivalent."""
    s = _state()
    if not semantic.available():
        return err("extraction_failed", "find_similar needs the [semantic] extra (fastembed)")

    # Exclude the seed itself from candidates by BOTH its requested url and (post-)redirect
    # final_url - a candidate equal to either is the seed, not a "similar" page.
    seed_urls: set[str] = set()
    try:
        if _is_url(url_or_text):
            try:
                validate_url(url_or_text)
                res = await fetch(url_or_text, client=s.client, browser=s.browser,
                                  throttle=s.throttle)
            except SSRFError as e:
                return err("ssrf_blocked", "URL blocked by SSRF guard", _safe_detail(e))
            except FetchError as e:
                return err("fetch_failed", "fetch failed", _safe_detail(e))
            art = extract_article(res["html"], res["final_url"])
            seed_text = f"{art['title'] or ''} {(art['content'] or res['html'])[:3000]}"
            query = art["title"] or (art["content"] or "")[:120] or url_or_text
            seed_urls = {res["final_url"], url_or_text}
        else:
            seed_text = url_or_text
            query = url_or_text[:200]

        try:
            found = await searxng_search(query, count=max(count * 2, 10))
        except SearchError as e:
            code = "no_results" if e.code == "no_results" else "search_backend_down"
            return err(code, "find_similar search failed", _safe_detail(e))

        cands = [r for r in found["results"] if r.get("url") not in seed_urls]
        if not cands:
            return err("no_results", "no similar candidates found")
        docs = [f"{c.get('title', '')} {c.get('snippet', '')}" for c in cands]
        sims = semantic.similarities(seed_text, docs)

        ranked = sorted(zip(cands, sims, strict=False), key=lambda x: -x[1])[:count]
        return {
            "seed": url_or_text,
            "results": [{**c, "score": round(float(sc), 4)} for c, sc in ranked],
            "count": len(ranked),
        }
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("extraction_failed", "find_similar failed", _safe_detail(e))


async def github_search(
    query: str, mode: str = "repositories", language: str | None = None,
    sort: str | None = None, order: str = "desc", limit: int = 10,
) -> dict:
    """Structured GitHub search - repos/code/issues with stars/language/sort. `code` mode needs
    GITHUB_TOKEN; optional token raises rate limits. Complements `search(category='it')`."""
    s = _state()
    ck = s.cache.key(
        "github:" + query,
        {"mode": mode, "language": language, "sort": sort, "order": order, "limit": limit},
    )
    cached = s.cache.get(ck, ttl_for("search"))
    if cached is not None:
        return {**cached, "from_cache": True}

    try:
        res = await _gh_search(
            query, mode=mode, language=language, sort=sort, order=order, limit=limit,
            client=s.client,
        )
    except GitHubSearchError as e:
        code = e.code if e.code in {"search_backend_down", "no_results", "schema_invalid"} \
            else "search_backend_down"
        return err(code, "github search failed", _safe_detail(e))
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("search_backend_down", "github search failed", _safe_detail(e))

    if isinstance(res, dict) and "code" not in res:
        res["from_cache"] = False
        s.cache.put(ck, res, source="search")
    return res


async def scholar_search(
    query: str, limit: int = 10, year_from: int | None = None, open_access: bool = False
) -> dict:
    """Structured academic-paper search (Semantic Scholar -> CrossRef fallback): title, authors,
    year, venue, citations, DOI, abstract, open-access PDF. Free, no key (optional S2 key)."""
    s = _state()
    ck = s.cache.key(
        "scholar:" + query,
        {"limit": limit, "year_from": year_from, "open_access": open_access},
    )
    cached = s.cache.get(ck, ttl_for("general"))
    if cached is not None:
        return {**cached, "from_cache": True}

    try:
        res = await _scholar_search(
            query, limit=limit, year_from=year_from, open_access=open_access, client=s.client
        )
    except ScholarError as e:
        code = "no_results" if e.code == "no_results" else "search_backend_down"
        return err(code, "scholar search failed", _safe_detail(e))
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("search_backend_down", "scholar search failed", _safe_detail(e))

    if isinstance(res, dict) and "code" not in res:
        res["from_cache"] = False
        s.cache.put(ck, res, source="general")
    return res


async def smart_search(query: str, count: int = 10) -> dict:
    """Auto-route a query to the best backend (deterministic classifier, no LLM): github / scholar
    / news / it / general. Returns {query, route, reason, result}; calls the matched tool."""
    routed = classify(query)
    r = routed["route"]
    if r == "github":
        result = await github_search(query, mode="repositories", limit=count)
    elif r == "scholar":
        result = await scholar_search(query, limit=count)
    elif r == "news":
        result = await search(query, category="news", count=count)
    elif r == "it":
        result = await search(query, category="it", count=count)
    else:
        result = await search(query, count=count)
    return {"query": query, "route": r, "reason": routed["reason"], "result": result}


async def watch(
    url: str, webhook: str, interval_minutes: int = 60, selector: str | None = None
) -> dict:
    """Register a watch: poll `url` (optionally a CSS/XPath `selector`) every `interval_minutes`
    and POST a change event to `webhook` (e.g. Telegram). Webhook is SSRF-guarded at delivery."""
    s = _state()
    try:
        validate_url(url)
        validate_url(webhook)
    except SSRFError as e:
        return err("ssrf_blocked", "url/webhook blocked by SSRF guard", _safe_detail(e))
    try:
        w = s.watch_store.add(url, selector, max(60, int(interval_minutes * 60)), webhook)
    except Exception as e:  # noqa: BLE001 - watch-store persistence (OSError) must not raise
        return err("fetch_failed", "could not register watch", _safe_detail(e))
    return {"id": w.id, "url": w.url, "selector": w.selector, "interval_s": w.interval_s,
            "webhook": w.webhook}


async def list_watches() -> dict:
    """List registered watches."""
    s = _state()
    try:
        watches = [asdict(w) for w in s.watch_store.list()]
    except Exception as e:  # noqa: BLE001 - never raise to client
        return err("fetch_failed", "could not list watches", _safe_detail(e))
    return {"watches": watches, "count": len(watches)}


async def unwatch(watch_id: str) -> dict:
    """Remove a watch by id."""
    s = _state()
    try:
        removed = s.watch_store.remove(watch_id)
    except Exception as e:  # noqa: BLE001 - watch-store persistence (OSError) must not raise
        return err("fetch_failed", "could not remove watch", _safe_detail(e))
    return {"id": watch_id, "removed": removed}


_TOOL_CALLS: dict[str, int] = {}


class _MetricsMiddleware(Middleware):
    """Count MCP tool invocations and track per-tool latency. Signature-safe (no wrapping)."""

    async def on_call_tool(self, context, call_next):
        name = getattr(getattr(context, "message", None), "name", "unknown")
        _TOOL_CALLS[name] = _TOOL_CALLS.get(name, 0) + 1

        t0 = time.perf_counter()
        try:
            result = await call_next(context)
        finally:
            elapsed = time.perf_counter() - t0
            lat = _tool_latencies.setdefault(name, deque(maxlen=HEALTH_LATENCY_BUCKETS))
            lat.append(elapsed)
        return result


# The registered tool set (20 tools). Exposed as a module constant so the offline test
# suite can assert the count without spinning up the in-memory MCP client (browser-marked).
TOOLS = (
    read, search, smart_search, read_pdf, scrape, batch_read, extract_structured,
    crawl, screenshot, research, map_urls, find_similar, github_search, scholar_search,
    watch, list_watches, unwatch,
    forexfactory_calendar, cot_report, news_sentiment_feed,
)

mcp = FastMCP(name="argus", instructions=INSTRUCTIONS, lifespan=lifespan, auth=_build_auth(),
              middleware=[_MetricsMiddleware()])
for _fn in TOOLS:
    mcp.tool(_fn)


def _healthy() -> bool:
    return _S is not None and _S.browser is not None and _S.browser._crawler is not None


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    """Liveness + readiness probe. Unauthenticated, cheap (no render)."""
    ok = _healthy()
    body = {
        "status": "ok" if ok else "degraded",
        "browser": ok,
        "uptime_seconds": round(time.monotonic() - _STARTUP_TIME, 1) if _STARTUP_TIME else None,
    }
    s = _S
    if s is not None:
        # cache stats
        try:
            cur = s.cache.conn.execute("SELECT COUNT(*) FROM entries").fetchone()
            body["cache_entries"] = cur[0] if cur else 0
        except Exception:
            body["cache_entries"] = None
        # watch count
        try:
            body["watch_count"] = len(s.watch_store.list()) if s.watch_store else 0
        except Exception:
            body["watch_count"] = None
        # per-tool latency percentiles (last N samples)
        body["tool_latencies"] = {
            name: _latency_percentiles(name) for name in _TOOL_CALLS
        }
    return JSONResponse(body, status_code=200 if ok else 503)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(_request):
    """Prometheus exposition format."""
    s = _S
    active = s.browser.active_contexts if (s and s.browser) else 0
    lines = [
        "# HELP argus_up 1 if the server process is up",
        "# TYPE argus_up gauge",
        "argus_up 1",
        "# HELP argus_browser_up 1 if the shared Chromium is live",
        "# TYPE argus_browser_up gauge",
        f"argus_browser_up {int(_healthy())}",
        "# HELP argus_active_contexts in-flight browser pages (semaphore in use)",
        "# TYPE argus_active_contexts gauge",
        f"argus_active_contexts {active}",
        "# HELP argus_tool_requests_total MCP tool invocations since start",
        "# TYPE argus_tool_requests_total counter",
    ]
    total = 0
    for name, n in sorted(_TOOL_CALLS.items()):
        lines.append(f'argus_tool_requests_total{{tool="{name}"}} {n}')
        total += n
    lines.append(f"argus_tool_requests_total{{tool=\"_all\"}} {total}")

    # Latency histogram buckets (Prometheus-style)
    lines.append("# HELP argus_tool_latency_seconds MCP tool call latency")
    lines.append("# TYPE argus_tool_latency_seconds summary")
    for name in sorted(_TOOL_CALLS):
        pct = _latency_percentiles(name)
        if pct:
            m = "argus_tool_latency_seconds"
            lines.append(f'{m}{{tool="{name}",quantile="0.5"}} {pct["p50"]}')
            lines.append(f'{m}{{tool="{name}",quantile="0.9"}} {pct["p90"]}')
            lines.append(f'{m}{{tool="{name}",quantile="0.99"}} {pct["p99"]}')
            lines.append(f'{m}_count{{tool="{name}"}} {pct["count"]}')

    return PlainTextResponse("\n".join(lines) + "\n")


# ASGI app for `uvicorn argus.server:app` (Streamable HTTP at /mcp). Zero client process.
app = mcp.http_app(path="/mcp")


if __name__ == "__main__":
    mcp.run()
