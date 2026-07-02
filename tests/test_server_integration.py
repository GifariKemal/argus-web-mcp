import pytest

from argus import server
from conftest import BASE


async def test_read_happy_then_cache(app_state):
    r1 = await server.read(f"{BASE}/article")
    assert r1["from_cache"] is False
    assert r1["render_path"] == "static"  # rich enough, no escalation
    assert "Gold prices" in r1["content"]
    assert r1["metadata"]["word_count"] > 0
    r2 = await server.read(f"{BASE}/article")
    assert r2["from_cache"] is True
    assert r2["content"] == r1["content"]


async def test_read_ssrf_blocked_returns_error_not_raise(app_state):
    r = await server.read("http://169.254.169.254/latest/meta-data/")
    assert r["code"] == "ssrf_blocked"
    assert "error" in r


async def test_read_bad_scheme(app_state):
    r = await server.read("file:///etc/passwd")
    assert r["code"] == "ssrf_blocked"


async def test_read_thin_escalates_to_browser(app_state):
    r = await server.read(f"{BASE}/thin")
    assert r["render_path"] == "browser"
    assert "Rendered content" in r["content"]
    assert app_state.browser.calls == 1


async def test_read_empty_content_no_browser(app_state):
    app_state.browser = None  # no escalation -> thin page yields empty content
    r = await server.read(f"{BASE}/thin")
    assert r["code"] == "empty_content"


async def test_read_pdf(app_state):
    r = await server.read_pdf(f"{BASE}/doc.pdf")
    assert r["pages_total"] == 2
    assert "alpha" in r["content"]
    assert r["source"].endswith("/doc.pdf")


async def test_read_pdf_page_slice(app_state):
    r = await server.read_pdf(f"{BASE}/doc.pdf", pages="1")
    assert r["pages_returned"] == 1
    assert "alpha" in r["content"]
    assert "beta" not in r["content"]


async def test_read_pdf_not_pdf(app_state):
    r = await server.read_pdf(f"{BASE}/article")  # HTML, not a PDF
    assert r["code"] == "not_pdf"


async def test_read_pdf_local_path(app_state, tmp_path, monkeypatch):
    from conftest import PDF_BYTES

    monkeypatch.setenv("ARGUS_ALLOW_LOCAL_PDF", "1")  # local-path reads opt-in
    p = tmp_path / "local.pdf"
    p.write_bytes(PDF_BYTES)
    r = await server.read_pdf(str(p))
    assert r["pages_total"] == 2


async def test_read_pdf_local_missing(app_state, monkeypatch):
    monkeypatch.setenv("ARGUS_ALLOW_LOCAL_PDF", "1")
    r = await server.read_pdf("C:/nope/missing-xyz.pdf")
    assert r["code"] == "fetch_failed"


async def test_read_pdf_local_disabled_by_default(app_state, monkeypatch):
    monkeypatch.delenv("ARGUS_ALLOW_LOCAL_PDF", raising=False)
    r = await server.read_pdf("C:/whatever.pdf")
    assert r["code"] == "fetch_failed"
    assert "disabled" in r["error"]


async def test_scrape_uses_browser(app_state):
    r = await server.scrape(f"{BASE}/article", screenshot=True)
    assert r["render_path"] == "browser"
    assert r["screenshot"] == "BASE64PNG"
    assert "Rendered content" in r["content"]


async def test_scrape_ssrf_blocked(app_state):
    r = await server.scrape("http://127.0.0.1/admin")
    assert r["code"] == "ssrf_blocked"


async def test_batch_read_partial_failure(app_state):
    urls = [f"{BASE}/article", "http://169.254.169.254/", f"{BASE}/article"]
    r = await server.batch_read(urls)
    assert r["succeeded"] == 2
    assert r["failed"] == 1
    blocked = [x for x in r["results"] if not x["ok"]][0]
    assert blocked["error"]["code"] == "ssrf_blocked"


async def test_batch_read_cap_note(app_state):
    urls = [f"{BASE}/article"] * 205
    r = await server.batch_read(urls)
    assert "note" in r
    assert len(r["results"]) == 200


async def test_extract_structured_selector(app_state):
    schema = {
        "title": "h1",
        "price": {"selector": ".price", "attr": "data-v"},
        "items": {"selector": "li", "many": True},
    }
    r = await server.extract_structured(f"{BASE}/struct", schema)
    assert r["valid"] is True
    assert r["data"]["title"] == "Widget"
    assert r["data"]["price"] == "9.99"
    assert r["data"]["items"] == ["a", "b"]
    assert r["mode_used"] == "selector"


async def test_extract_structured_multi_and_ssrf(app_state):
    schema = {"title": "h1"}
    r = await server.extract_structured([f"{BASE}/struct", "http://10.0.0.1/x"], schema)
    assert "results" in r
    assert r["results"][0]["valid"] is True
    assert r["results"][1]["code"] == "ssrf_blocked"


async def test_extract_structured_bad_schema(app_state):
    r = await server.extract_structured(f"{BASE}/struct", {})
    assert r["code"] == "schema_invalid"




async def test_search_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def fake_search(query, **kw):
        calls["n"] += 1
        return {
            "query": query,
            "results": [{"title": "T", "url": "http://x/1", "snippet": "s", "engine": "duck"}],
            "count": 1,
            "engines_used": ["duck"],
        }

    monkeypatch.setattr(server, "searxng_search", fake_search)
    r1 = await server.search("gold price")
    assert r1["from_cache"] is False
    assert r1["count"] == 1
    r2 = await server.search("gold price")
    assert r2["from_cache"] is True
    assert calls["n"] == 1  # second served from cache


async def test_search_backend_down(app_state, monkeypatch):
    async def boom(query, **kw):
        raise server.SearchError("search_backend_down", "down")

    monkeypatch.setattr(server, "searxng_search", boom)
    r = await server.search("anything")
    assert r["code"] == "search_backend_down"


async def test_search_no_results(app_state, monkeypatch):
    async def empty(query, **kw):
        raise server.SearchError("no_results", "none")

    monkeypatch.setattr(server, "searxng_search", empty)
    r = await server.search("zzz")
    assert r["code"] == "no_results"


async def test_news_sentiment_feed_does_not_route_through_guarded_client(app_state, monkeypatch):
    """REGRESSION (Bug 1): the news handler must NOT forward the SSRF-guarded
    s.client into the internal loopback SearXNG search.

    Before the fix the handler passed client=s.client; the guard blocked the
    127.0.0.1:8888 SearXNG host and the call failed with
    {code:'extraction_failed', detail:'SSRFError'}. We monkeypatch the REAL
    search seam (news.web_search) to record the client it receives and assert
    (a) the handler succeeds (no error dict) and (b) it was NOT handed s.client.
    """
    from argus.trading import news

    captured = {}

    async def fake_web_search(query, **kw):
        captured["kwargs"] = kw
        return {"query": query, "results": [
            {"title": "Gold rallies", "url": "https://ex.com/1", "content": "up", "engine": "n"},
        ], "count": 1}

    monkeypatch.setattr(news, "web_search", fake_web_search)

    out = await server.news_sentiment_feed("gold")

    assert "code" not in out  # not an error dict (was 'extraction_failed' before fix)
    assert out["count"] == 1
    # The guarded s.client must NOT have been forwarded to the SearXNG search.
    assert captured["kwargs"].get("client") is not app_state.client
    assert captured["kwargs"].get("client") is None


def test_instructions_under_2kb():
    assert len(server.INSTRUCTIONS.encode("utf-8")) < 2048


async def test_read_extract_media(app_state):
    r = await server.read(f"{BASE}/article", extract_media=True)
    assert "links" in r and "images" in r
    assert isinstance(r["links"], list) and isinstance(r["images"], list)


async def test_research_highlights(app_state, monkeypatch):
    async def fake_research(query, **kw):
        return {"query": query, "mode": "deep", "sources": [
            {"url": "http://x/1", "title": "T", "content": "Gold is driven by real yields. "
             "Central banks buy gold. Inflation matters too.", "word_count": 12}],
            "failed": [], "count": 1, "source_count_requested": 5}

    monkeypatch.setattr(server, "_research", fake_research)
    monkeypatch.setattr(server.semantic, "available", lambda: True)
    monkeypatch.setattr(server.semantic, "top_sentences",
                        lambda q, t, top_k=3: ["Gold is driven by real yields."])
    r = await server.research("gold drivers", highlights=True)
    assert r["sources"][0]["highlights"] == ["Gold is driven by real yields."]


def test_auth_jwt_precedence(monkeypatch):
    monkeypatch.setenv("ARGUS_JWT_JWKS_URI", "https://issuer.example/.well-known/jwks.json")
    monkeypatch.setenv("ARGUS_TOKEN", "ignored-when-jwt-set")
    auth = server._build_auth()
    assert auth is not None
    assert type(auth).__name__ == "JWTVerifier"


async def test_metrics_middleware_counts():
    server._TOOL_CALLS.clear()
    mw = server._MetricsMiddleware()

    class _Ctx:
        class message:
            name = "read"

    async def _next(_c):
        return "ok"

    out = await mw.on_call_tool(_Ctx(), _next)
    assert out == "ok"
    assert server._TOOL_CALLS["read"] == 1


async def test_metrics_middleware_no_longer_requires_state():
    """The middleware must work with no lifespan state (_S is None) — it no longer
    calls _state() for rate limiting. It only counts calls and records latency."""
    assert server._S is None
    server._TOOL_CALLS.clear()
    server._tool_latencies.clear()
    mw = server._MetricsMiddleware()

    class _Ctx:
        class message:
            name = "scrape"

    async def _next(_c):
        return {"ok": True}

    out = await mw.on_call_tool(_Ctx(), _next)
    assert out == {"ok": True}
    assert server._TOOL_CALLS["scrape"] == 1
    # latency sample was recorded for the tool
    assert len(server._tool_latencies["scrape"]) == 1


def test_latency_percentiles_empty():
    server._tool_latencies.pop("nonesuch", None)
    assert server._latency_percentiles("nonesuch") == {}


def test_latency_percentiles_known_samples():
    from collections import deque

    # 1..100 -> p50=50, p90=90, p99=99 with int(n*k) indexing on the sorted list.
    server._tool_latencies["sample"] = deque(float(i) for i in range(1, 101))
    pct = server._latency_percentiles("sample")
    assert pct["count"] == 100
    assert pct["min"] == 1.0
    assert pct["max"] == 100.0
    assert pct["p50"] == 51.0  # s[int(100*0.5)] = s[50] = 51
    assert pct["p90"] == 91.0  # s[int(100*0.9)] = s[90] = 91
    assert pct["p99"] == 100.0  # s[int(100*0.99)] = s[99] = 100
    server._tool_latencies.pop("sample", None)


async def test_read_with_throttle_path(app_state):
    from argus.fetch.throttle import HostThrottle

    app_state.throttle = HostThrottle(min_interval=0.0)  # no wait, exercises acquire+record
    r = await server.read(f"{BASE}/article")
    assert r["from_cache"] is False
    assert "Gold prices" in r["content"]


async def test_health_and_metrics_endpoints():
    class _B:
        _crawler = object()
        active_contexts = 3

    server._S = server.State(client=None, cache=None, browser=_B())
    try:
        h = await server.health(None)
        assert h.status_code == 200
        m = await server.metrics(None)
        body = bytes(m.body).decode()
        assert "argus_up 1" in body
        assert "argus_browser_up 1" in body
        assert "argus_active_contexts 3" in body
    finally:
        server._S = None


async def test_health_degraded_when_no_browser():
    server._S = None
    h = await server.health(None)
    assert h.status_code == 503


def test_auth_disabled_without_token(monkeypatch):
    monkeypatch.delenv("ARGUS_TOKEN", raising=False)
    assert server._build_auth() is None


def test_auth_enabled_with_token(monkeypatch):
    monkeypatch.setenv("ARGUS_TOKEN", "secret-xyz")
    assert server._build_auth() is not None


async def test_screenshot_tool(app_state):
    r = await server.screenshot(f"{BASE}/article")
    assert r["format"] == "png"
    assert r["screenshot"] == "BASE64PNG"


async def test_screenshot_ssrf_blocked(app_state):
    r = await server.screenshot("http://10.0.0.1/x")
    assert r["code"] == "ssrf_blocked"


async def test_crawl_no_browser(app_state):
    app_state.browser = None
    r = await server.crawl(f"{BASE}/article")
    assert r["code"] == "render_failed"


async def test_crawl_delegates(app_state, monkeypatch):
    async def fake_deep_crawl(seed, **kw):
        return {"pages": [{"url": seed, "title": "t", "content": "c", "depth": 0}],
                "link_graph": {}, "count": 1}

    monkeypatch.setattr(server, "deep_crawl", fake_deep_crawl)
    r = await server.crawl(f"{BASE}/article", depth=1, max_pages=3)
    assert r["count"] == 1


async def test_extract_structured_auto_falls_back_to_llm(app_state, monkeypatch):
    # selectors miss -> invalid -> auto falls back to LLM when available
    monkeypatch.setattr(server, "llm_available", lambda: True)

    async def fake_llm(content, schema, prompt=None, client=None):
        return {"data": {"headline": "From LLM"}, "valid": True}

    monkeypatch.setattr(server, "extract_llm", fake_llm)
    r = await server.extract_structured(f"{BASE}/struct", {"headline": ".does-not-exist"})
    assert r["mode_used"] == "llm"
    assert r["data"]["headline"] == "From LLM"


async def test_extract_structured_llm_mode_needs_key(app_state, monkeypatch):
    monkeypatch.setattr(server, "llm_available", lambda: False)
    r = await server.extract_structured(f"{BASE}/struct", {"x": "str"}, mode="llm")
    assert r["code"] == "extraction_failed"


async def test_forexfactory_tool_wraps_error(app_state, monkeypatch):
    async def boom(date_range=None, client=None):
        raise RuntimeError("feed down")

    monkeypatch.setattr(server, "_ff_calendar", boom)
    r = await server.forexfactory_calendar()
    assert r["code"] == "fetch_failed"


async def test_research_delegates(app_state, monkeypatch):
    async def fake_research(query, **kw):
        return {"query": query, "sources": [{"url": "http://x/1", "title": "T",
                "content": "full body", "word_count": 2, "render_path": "static"}],
                "failed": [], "count": 1, "source_count_requested": kw.get("max_sources", 5)}

    monkeypatch.setattr(server, "_research", fake_research)
    r = await server.research("esp-claw", max_sources=3)
    assert r["count"] == 1
    assert r["sources"][0]["content"] == "full body"


async def test_research_quick_mode(app_state, monkeypatch):
    async def fake_research(query, **kw):
        assert kw.get("mode") == "quick"
        return {"query": query, "mode": "quick", "sources": [{"url": "http://x/1",
                "title": "T", "snippet": "s"}], "failed": [], "count": 1,
                "source_count_requested": kw.get("max_sources", 5)}

    monkeypatch.setattr(server, "_research", fake_research)
    r = await server.research("x", mode="quick")
    assert r["mode"] == "quick"
    assert "content" not in r["sources"][0]


async def test_research_invalid_mode(app_state, monkeypatch):
    async def fake_research(query, **kw):
        raise ValueError("unknown research mode: bogus")

    monkeypatch.setattr(server, "_research", fake_research)
    r = await server.research("x", mode="bogus")
    assert r["code"] == "schema_invalid"


async def test_research_search_backend_down(app_state, monkeypatch):
    async def boom(query, **kw):
        raise server.SearchError("search_backend_down", "engines suspended")

    monkeypatch.setattr(server, "_research", boom)
    r = await server.research("anything")
    assert r["code"] == "search_backend_down"


async def test_research_unexpected_error_maps_to_extraction_failed(app_state, monkeypatch):
    # A bare RuntimeError (not ValueError/SearchError) hits the catch-all and is
    # surfaced as a structured extraction_failed error, never raised to the client.
    async def boom(query, **kw):
        raise RuntimeError("synthesis blew up")

    monkeypatch.setattr(server, "_research", boom)
    r = await server.research("anything")
    assert r["code"] == "extraction_failed"


async def test_research_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def fake_research(query, **kw):
        calls["n"] += 1
        return {"query": query, "sources": [{"url": "http://x/1", "title": "T",
                "content": "full body", "word_count": 2, "render_path": "static"}],
                "failed": [], "count": 1, "source_count_requested": kw.get("max_sources", 5)}

    monkeypatch.setattr(server, "_research", fake_research)
    r1 = await server.research("esp-claw", max_sources=3)
    assert r1["from_cache"] is False
    r2 = await server.research("esp-claw", max_sources=3)
    assert r2["from_cache"] is True
    assert calls["n"] == 1  # second served from cache


async def test_research_error_not_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def boom(query, **kw):
        calls["n"] += 1
        raise server.SearchError("search_backend_down", "engines suspended")

    monkeypatch.setattr(server, "_research", boom)
    r1 = await server.research("anything")
    assert r1["code"] == "search_backend_down"
    r2 = await server.research("anything")
    assert r2["code"] == "search_backend_down"
    assert calls["n"] == 2  # error not cached -> backend hit again


async def test_scholar_search_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def fake(query, **kw):
        calls["n"] += 1
        return {"query": query, "source": "crossref", "count": 1, "results": [
            {"title": "Attention", "authors": ["A"], "year": 2017, "citations": 1,
             "doi": "10.x/y", "url": "http://x", "abstract": "a", "venue": "v",
             "open_access_pdf": None}]}

    monkeypatch.setattr(server, "_scholar_search", fake)
    r1 = await server.scholar_search("attention", limit=3)
    assert r1["from_cache"] is False
    r2 = await server.scholar_search("attention", limit=3)
    assert r2["from_cache"] is True
    assert calls["n"] == 1


async def test_scholar_search_error_not_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def boom(query, **kw):
        calls["n"] += 1
        raise server.ScholarError("no_results", "none")

    monkeypatch.setattr(server, "_scholar_search", boom)
    r1 = await server.scholar_search("zzz")
    assert r1["code"] == "no_results"
    r2 = await server.scholar_search("zzz")
    assert r2["code"] == "no_results"
    assert calls["n"] == 2


async def test_github_search_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def fake_gh(query, **kw):
        calls["n"] += 1
        return {"query": query, "mode": kw.get("mode", "repositories"), "total_count": 1,
                "results": [{"full_name": "jlowin/fastmcp", "stars": 9000}], "count": 1}

    monkeypatch.setattr(server, "_gh_search", fake_gh)
    r1 = await server.github_search("fastmcp", language="python")
    assert r1["from_cache"] is False
    r2 = await server.github_search("fastmcp", language="python")
    assert r2["from_cache"] is True
    assert calls["n"] == 1


async def test_github_search_error_not_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def boom(query, **kw):
        calls["n"] += 1
        raise server.GitHubSearchError("search_backend_down", "rate limit")

    monkeypatch.setattr(server, "_gh_search", boom)
    r1 = await server.github_search("x")
    assert r1["code"] == "search_backend_down"
    r2 = await server.github_search("x")
    assert r2["code"] == "search_backend_down"
    assert calls["n"] == 2


async def test_map_urls_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def fake_map(url, **kw):
        calls["n"] += 1
        return {"url": url, "urls": [f"{url}a", f"{url}b"], "count": 2, "source": "sitemap"}

    monkeypatch.setattr(server, "map_site", fake_map)
    r1 = await server.map_urls(f"{BASE}/")
    assert r1["from_cache"] is False
    r2 = await server.map_urls(f"{BASE}/")
    assert r2["from_cache"] is True
    assert calls["n"] == 1


async def test_map_urls_error_not_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def boom(url, **kw):
        calls["n"] += 1
        raise RuntimeError("unexpected explosion")

    monkeypatch.setattr(server, "map_site", boom)
    r1 = await server.map_urls(f"{BASE}/")
    assert r1["code"] == "fetch_failed"
    r2 = await server.map_urls(f"{BASE}/")
    assert r2["code"] == "fetch_failed"
    assert calls["n"] == 2


async def test_news_feed_delegates(app_state, monkeypatch):
    seen = {}

    async def fake_feed(query, since=None, sentiment=False, client=None):
        seen["client"] = client
        return {"query": query, "items": [{"title": "n", "url": "http://x/1", "snippet": "s"}],
                "count": 1}

    monkeypatch.setattr(server, "_news_feed", fake_feed)
    r = await server.news_sentiment_feed("gold")
    assert r["count"] == 1
    # Bug 1 fix: the news ranker fetches the INTERNAL loopback SearXNG (127.0.0.1:8888),
    # which the external-URL SSRF guard blocks by design. The handler must NOT forward
    # the guarded s.client (doing so raised SSRFError -> 'extraction_failed' on every
    # call); the search layer creates its own plain client for the trusted backend,
    # exactly like the working search() handler. The SSRF gate stays intact for every
    # genuinely external fetch.
    assert seen["client"] is None
    assert seen["client"] is not app_state.client


@pytest.mark.slow
async def test_read_pdf_quality_docling(app_state, monkeypatch):
    from conftest import PDF_BYTES

    p = pytest.importorskip("docling")  # noqa: F841
    import os
    import tempfile

    monkeypatch.setenv("ARGUS_ALLOW_LOCAL_PDF", "1")  # local-path reads opt-in (LFI lockdown)
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.write(fd, PDF_BYTES)
    os.close(fd)
    r = await server.read_pdf(path, mode="quality")
    assert "content" in r and r.get("metadata", {}).get("engine") == "docling"


@pytest.mark.browser
async def test_inmemory_client_lists_all_tools():
    from fastmcp import Client

    async with Client(server.mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "read", "search", "smart_search", "read_pdf", "scrape", "batch_read",
        "extract_structured", "crawl", "screenshot", "research", "map_urls", "find_similar",
        "github_search", "scholar_search", "watch", "list_watches", "unwatch",
        "forexfactory_calendar", "cot_report", "news_sentiment_feed",
    }


async def test_watch_register_list_unwatch(app_state):
    r = await server.watch("https://example.com/", "https://hooks.example/abc", interval_minutes=30)
    assert r["interval_s"] == 1800 and r["id"]
    lst = await server.list_watches()
    assert lst["count"] == 1 and lst["watches"][0]["url"] == "https://example.com/"
    rm = await server.unwatch(r["id"])
    assert rm["removed"] is True
    assert (await server.list_watches())["count"] == 0


async def test_watch_bad_webhook_scheme(app_state):
    r = await server.watch("https://example.com/", "file:///etc/passwd")
    assert r["code"] == "ssrf_blocked"


async def test_watch_store_oserror_returns_err_not_raise(app_state, monkeypatch):
    """R1: a watch-store persistence failure (OSError) must come back as a structured
    err(...) dict, never propagate to the client."""
    def boom_add(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(app_state.watch_store, "add", boom_add)
    r = await server.watch("https://example.com/", "https://hooks.example/abc")
    assert isinstance(r, dict)
    assert r["code"] == "fetch_failed"
    # Sec-F2: the internal exception message ("disk full") must NOT leak to the client.
    assert "disk full" not in (r.get("detail") or "")


async def test_unwatch_store_oserror_returns_err_not_raise(app_state, monkeypatch):
    def boom_remove(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(app_state.watch_store, "remove", boom_remove)
    r = await server.unwatch("someid")
    assert r["code"] == "fetch_failed"


async def test_list_watches_oserror_returns_err_not_raise(app_state, monkeypatch):
    def boom_list(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(app_state.watch_store, "list", boom_list)
    r = await server.list_watches()
    assert r["code"] == "fetch_failed"


def test_registered_tool_set_is_twenty():
    """Offline guard (no `browser` marker): the registration tuple holds exactly 20 tools,
    so the offline suite enforces the count even though the in-memory MCP listing is
    browser-marked."""
    assert len(server.TOOLS) == 20
    assert len({fn.__name__ for fn in server.TOOLS}) == 20


async def test_scholar_search_delegates(app_state, monkeypatch):
    async def fake(query, **kw):
        return {"query": query, "source": "crossref", "count": 1, "results": [
            {"title": "Attention Is All You Need", "authors": ["A Vaswani"], "year": 2017,
             "citations": 100000, "doi": "10.x/y", "url": "http://x", "abstract": "a",
             "venue": "NeurIPS", "open_access_pdf": None}]}

    monkeypatch.setattr(server, "_scholar_search", fake)
    r = await server.scholar_search("attention", limit=3)
    assert r["count"] == 1
    assert r["results"][0]["citations"] == 100000


async def test_scholar_search_error_wrapped(app_state, monkeypatch):
    async def boom(query, **kw):
        raise server.ScholarError("no_results", "none")

    monkeypatch.setattr(server, "_scholar_search", boom)
    r = await server.scholar_search("zzz")
    assert r["code"] == "no_results"


async def test_smart_search_routes(app_state, monkeypatch):
    calls = {}

    async def fake_search(query, **kw):
        calls["search"] = kw.get("category", "general")
        return {"query": query, "results": [], "count": 0, "engines_used": []}

    async def fake_gh(query, **kw):
        calls["gh"] = True
        return {"query": query, "results": [{"full_name": "a/b"}], "count": 1}

    async def fake_scholar(query, **kw):
        calls["scholar"] = True
        return {"query": query, "results": [{"title": "p"}], "count": 1, "source": "x"}

    monkeypatch.setattr(server, "searxng_search", fake_search)
    monkeypatch.setattr(server, "_gh_search", fake_gh)
    monkeypatch.setattr(server, "_scholar_search", fake_scholar)

    r1 = await server.smart_search("fastmcp github repository")
    assert r1["route"] == "github" and "result" in r1
    r2 = await server.smart_search("transformer attention paper arxiv")
    assert r2["route"] == "scholar"
    r3 = await server.smart_search("best pizza in town")
    assert r3["route"] == "general"


async def test_github_search_delegates(app_state, monkeypatch):
    async def fake_gh(query, **kw):
        return {"query": query, "mode": kw.get("mode", "repositories"), "total_count": 1,
                "results": [{"full_name": "jlowin/fastmcp", "stars": 9000, "language": "Python"}],
                "count": 1}

    monkeypatch.setattr(server, "_gh_search", fake_gh)
    r = await server.github_search("fastmcp", language="python")
    assert r["count"] == 1
    assert r["results"][0]["full_name"] == "jlowin/fastmcp"


async def test_github_search_code_needs_token(app_state, monkeypatch):
    async def fake_gh(query, **kw):
        raise server.GitHubSearchError("schema_invalid", "code search requires GITHUB_TOKEN")

    monkeypatch.setattr(server, "_gh_search", fake_gh)
    r = await server.github_search("def main", mode="code")
    assert r["code"] == "schema_invalid"


async def test_github_search_rate_limited(app_state, monkeypatch):
    async def fake_gh(query, **kw):
        raise server.GitHubSearchError("search_backend_down", "rate limit")

    monkeypatch.setattr(server, "_gh_search", fake_gh)
    r = await server.github_search("x")
    assert r["code"] == "search_backend_down"


async def test_map_urls_catch_all_returns_structured_error(app_state, monkeypatch):
    async def boom(url, **kw):
        raise RuntimeError("unexpected explosion")

    monkeypatch.setattr(server, "map_site", boom)
    r = await server.map_urls(f"{BASE}/")
    assert isinstance(r, dict)
    assert r["code"] == "fetch_failed"


async def test_find_similar_catch_all_returns_structured_error(app_state, monkeypatch):
    monkeypatch.setattr(server.semantic, "available", lambda: True)

    async def boom_search(query, **kw):
        raise RuntimeError("totally unexpected")

    monkeypatch.setattr(server, "searxng_search", boom_search)
    r = await server.find_similar("python web scraping")
    assert isinstance(r, dict)
    assert r["code"] == "extraction_failed"


async def test_find_similar_excludes_pre_redirect_seed_url(app_state, monkeypatch):
    monkeypatch.setattr(server.semantic, "available", lambda: True)

    # The seed /old 301-redirects to /article (final_url differs from the requested url).
    async def fake_search(query, **kw):
        return {"query": query, "results": [
            # candidate equal to the ORIGINAL (pre-redirect) seed url -> must be excluded
            {"title": "Same as seed", "url": f"{BASE}/old", "snippet": "x"},
            {"title": "Other page", "url": "http://a/2", "snippet": "y"},
        ], "count": 2, "engines_used": ["x"]}

    monkeypatch.setattr(server, "searxng_search", fake_search)
    monkeypatch.setattr(server.semantic, "similarities", lambda seed, docs: [0.5] * len(docs))
    r = await server.find_similar(f"{BASE}/old", count=5)
    urls = [x["url"] for x in r["results"]]
    assert f"{BASE}/old" not in urls
    assert "http://a/2" in urls


async def test_find_similar_needs_semantic(app_state, monkeypatch):
    monkeypatch.setattr(server.semantic, "available", lambda: False)
    r = await server.find_similar("python web scraping")
    assert r["code"] == "extraction_failed"


async def test_find_similar_ranks_by_semantic(app_state, monkeypatch):
    monkeypatch.setattr(server.semantic, "available", lambda: True)

    async def fake_search(query, **kw):
        return {"query": query, "results": [
            {"title": "Web crawling in Python", "url": "http://a/1", "snippet": "scrapy"},
            {"title": "Banana bread recipe", "url": "http://a/2", "snippet": "flour"},
        ], "count": 2, "engines_used": ["x"]}

    # sims aligned to docs order: first doc more similar
    monkeypatch.setattr(server, "searxng_search", fake_search)
    monkeypatch.setattr(server.semantic, "similarities", lambda seed, docs: [0.9, 0.1])
    r = await server.find_similar("python web scraping", count=2)
    assert r["count"] == 2
    assert r["results"][0]["url"] == "http://a/1"  # higher semantic score first
    assert r["results"][0]["score"] >= r["results"][1]["score"]


@pytest.mark.anyio
async def test_find_similar_clamps_invalid_count(app_state, monkeypatch):
    """count < 1 is clamped to 1 at the trust boundary (was: `[:negative]` wrong subset)."""
    monkeypatch.setattr(server.semantic, "available", lambda: True)
    seen = {}

    async def fake_search(query, **kw):
        seen["count"] = kw.get("count")
        return {"query": query, "results": [
            {"title": "a", "url": "http://a/1", "snippet": "x"},
            {"title": "b", "url": "http://a/2", "snippet": "y"},
            {"title": "c", "url": "http://a/3", "snippet": "z"},
        ], "count": 3, "engines_used": ["x"]}

    monkeypatch.setattr(server, "searxng_search", fake_search)
    monkeypatch.setattr(server.semantic, "similarities", lambda seed, docs: [0.9, 0.5, 0.1])
    r = await server.find_similar("python web scraping", count=-1)
    assert r["count"] == 1  # clamped to 1, not a negative-slice subset
    assert seen["count"] >= 10  # search overfetch stays bounded/sane, not count*2 of -1


# --------------------------------------------------------------------------- #
# Round-6 hardening: tool caching, degraded-no-cache, specialist failover,     #
# error metrics, structured-error consistency                                  #
# --------------------------------------------------------------------------- #


async def test_read_pdf_url_cached(app_state):
    r1 = await server.read_pdf(f"{BASE}/doc.pdf")
    assert r1["from_cache"] is False
    r2 = await server.read_pdf(f"{BASE}/doc.pdf")
    assert r2["from_cache"] is True
    assert r2["content"] == r1["content"]


async def test_read_pdf_unknown_mode_schema_invalid(app_state):
    r = await server.read_pdf(f"{BASE}/doc.pdf", mode="figures")
    assert r["code"] == "schema_invalid"


async def test_read_pdf_bad_pages_schema_invalid(app_state):
    r = await server.read_pdf(f"{BASE}/doc.pdf", pages="abc")
    assert r["code"] == "schema_invalid"
    r2 = await server.read_pdf(f"{BASE}/doc.pdf", pages="99")
    assert r2["code"] == "schema_invalid"


async def test_forexfactory_cached_and_stale_not_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def fake_ff(date_range, client=None):
        calls["n"] += 1
        return {"events": [], "count": 0, "source": "s", "stale": False}

    monkeypatch.setattr(server, "_ff_calendar", fake_ff)
    r1 = await server.forexfactory_calendar()
    r2 = await server.forexfactory_calendar()
    assert calls["n"] == 1
    assert r1["from_cache"] is False and r2["from_cache"] is True

    # stale bundles must NOT be re-served as fresh cache hits
    calls["n"] = 0

    async def stale_ff(date_range, client=None):
        calls["n"] += 1
        return {"events": [], "count": 0, "source": "s", "stale": True,
                "stale_age_seconds": 60}

    monkeypatch.setattr(server, "_ff_calendar", stale_ff)
    await server.forexfactory_calendar(date_range=["2026-01-01", "2026-01-02"])
    await server.forexfactory_calendar(date_range=["2026-01-01", "2026-01-02"])
    assert calls["n"] == 2  # no cache hit for stale results


async def test_cot_cached_and_error_code_passthrough(app_state, monkeypatch):
    calls = {"n": 0}

    async def fake_cot(report_type="legacy_futures", date=None, client=None):
        calls["n"] += 1
        return {"rows": [], "count": 0, "report_type": report_type, "source": "s",
                "identity_failures": 0, "bad_dates": 0}

    monkeypatch.setattr(server, "_cot_report", fake_cot)
    await server.cot_report()
    r2 = await server.cot_report()
    assert calls["n"] == 1
    assert r2["from_cache"] is True

    from argus.trading.cot import CotError

    async def bad_cot(report_type="x", date=None, client=None):
        raise CotError("cot_bad_report_type", "unknown report_type")

    monkeypatch.setattr(server, "_cot_report", bad_cot)
    r = await server.cot_report(report_type="nope")
    assert r["code"] == "cot_bad_report_type"  # no longer masked as fetch_failed


async def test_ff_bad_date_range_code_passthrough(app_state, monkeypatch):
    from argus.trading.forexfactory import ForexFactoryError

    async def bad_ff(date_range, client=None):
        raise ForexFactoryError("ff_bad_date_range", "bad bounds")

    monkeypatch.setattr(server, "_ff_calendar", bad_ff)
    r = await server.forexfactory_calendar(date_range=["junk"])
    assert r["code"] == "ff_bad_date_range"


async def test_news_feed_cached_but_degraded_not_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def fake_feed(query, since=None, sentiment=False):
        calls["n"] += 1
        return {"query": query, "items": [], "count": 0, "degraded": False}

    monkeypatch.setattr(server, "_news_feed", fake_feed)
    await server.news_sentiment_feed("gold")
    r2 = await server.news_sentiment_feed("gold")
    assert calls["n"] == 1 and r2["from_cache"] is True

    calls["n"] = 0

    async def degraded_feed(query, since=None, sentiment=False):
        calls["n"] += 1
        return {"query": query, "items": [], "count": 0, "degraded": True,
                "degraded_reason": "low_relevance"}

    monkeypatch.setattr(server, "_news_feed", degraded_feed)
    await server.news_sentiment_feed("silver")
    await server.news_sentiment_feed("silver")
    assert calls["n"] == 2  # degraded feed never cached


async def test_search_degraded_not_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def degraded_search(query, **kw):
        calls["n"] += 1
        return {"query": query, "results": [], "count": 0, "engines_used": [],
                "backend": "b", "degraded": True, "degraded_reason": "low_relevance"}

    monkeypatch.setattr(server, "searxng_search", degraded_search)
    await server.search("junk query")
    await server.search("junk query")
    assert calls["n"] == 2  # degraded result set never cached


async def test_smart_search_specialist_failover_to_general(app_state, monkeypatch):
    from argus.gh_search import GitHubSearchError

    async def rate_limited(*a, **k):
        raise GitHubSearchError("search_backend_down", "rate limit")

    async def general_ok(query, **kw):
        return {"query": query, "results": [{"title": "t", "url": "https://x.com/1",
                                             "snippet": "s", "engine": "ddg"}],
                "count": 1, "engines_used": ["ddg"], "backend": "b",
                "degraded": False, "degraded_reason": None}

    monkeypatch.setattr(server, "_gh_search", rate_limited)
    monkeypatch.setattr(server, "searxng_search", general_ok)
    out = await server.smart_search("fastmcp github repo")
    assert out["route"] == "general"
    assert out["degraded"] is True
    assert out["degraded_reason"] == "specialist_failover"
    assert "code" not in out["result"]


async def test_err_counts_metric_increments_and_exports(app_state):
    from argus.models import ERR_COUNTS

    before = ERR_COUNTS.get("ssrf_blocked", 0)
    r = await server.read("http://169.254.169.254/latest/meta-data")
    assert r["code"] == "ssrf_blocked"
    assert ERR_COUNTS.get("ssrf_blocked", 0) == before + 1

    class _B:
        _crawler = object()
        active_contexts = 0

    old = server._S
    server._S = server.State(client=None, cache=None, browser=_B())
    try:
        m = await server.metrics(None)
        body = bytes(m.body).decode()
        assert 'argus_tool_errors_total{code="ssrf_blocked"}' in body
    finally:
        server._S = old


async def test_extract_structured_auto_selector_raises_no_llm_returns_error(
    app_state, monkeypatch
):
    def boom(html, schema):
        raise RuntimeError("selector engine exploded")

    monkeypatch.setattr(server, "extract_selectors", boom)
    r = await server.extract_structured(f"{BASE}/struct", {"title": "h1::text"}, mode="auto")
    assert r is not None
    assert r["code"] == "extraction_failed"  # was: bare None returned to the client


async def test_cot_drift_flagged_result_not_cached(app_state, monkeypatch):
    calls = {"n": 0}

    async def drifted_cot(report_type="legacy_futures", date=None, client=None):
        calls["n"] += 1
        return {"rows": [{"report_date": "2026-01-01"}], "count": 1,
                "report_type": report_type, "source": "s",
                "identity_failures": 1, "bad_dates": 0}

    monkeypatch.setattr(server, "_cot_report", drifted_cot)
    await server.cot_report()
    await server.cot_report()
    assert calls["n"] == 2  # drift-flagged data never cached; each call retries upstream


# --------------------------------------------------------------------------- #
# gap-scan batch (0.4.1): category validation, research timeout, highlights, antibot
# --------------------------------------------------------------------------- #
async def test_search_invalid_category_schema_invalid(app_state, monkeypatch):
    """A typo'd category is rejected up front (like read_pdf modes), not coerced to
    'general' and cached under the wrong key. No backend call is made."""
    async def boom(*a, **k):
        raise AssertionError("searxng_search must not be called for an invalid category")

    monkeypatch.setattr(server, "searxng_search", boom)
    r = await server.search("anything", category="nwes")
    assert r["code"] == "schema_invalid"


async def test_research_wall_clock_timeout(app_state, monkeypatch):
    """research() is bounded by an overall wall clock (backfill waves can each cost
    ~timeout); a slow bundle returns a structured timeout error, not a 3x overrun."""
    import asyncio
    import time

    async def slow_research(*a, **k):
        await asyncio.sleep(5)
        return {"sources": []}

    monkeypatch.setattr(server, "_research", slow_research)
    t0 = time.monotonic()
    r = await server.research("q", timeout=0.05)
    dt = time.monotonic() - t0
    assert r["code"] == "fetch_failed" and "timed out" in r["error"].lower()
    assert dt < 1.0  # bounded ~timeout, not the full 5s sleep


async def test_research_highlights_use_full_precap_content(app_state, monkeypatch):
    """highlights must be computed from FULL pre-cap content (top sentence may sit past
    the cap), and the stashed _full_content must never leak into the payload."""
    captured = {}

    def fake_top(query, text, top_k=3):
        captured["text"] = text
        return ["hl-sentence"]

    monkeypatch.setattr(server.semantic, "available", lambda: True)
    monkeypatch.setattr(server.semantic, "top_sentences", fake_top)

    async def fake_research(*a, **k):
        return {"mode": "deep", "sources": [
            {"url": "u", "content": "CAP", "_full_content": "FULL pre-cap body text",
             "truncated": True, "full_chars": 22},
        ]}

    monkeypatch.setattr(server, "_research", fake_research)
    r = await server.research("q", highlights=True, max_chars_per_source=3)
    src = r["sources"][0]
    assert captured["text"] == "FULL pre-cap body text"  # full content, not the cap
    assert src["highlights"] == ["hl-sentence"]
    assert "_full_content" not in src  # stripped -> payload stays lean


async def test_research_strips_full_content_even_without_highlights(app_state, monkeypatch):
    """Even with highlights=False the pre-cap stash must be stripped (no payload bloat)."""
    async def fake_research(*a, **k):
        return {"mode": "deep", "sources": [
            {"url": "u", "content": "CAP", "_full_content": "FULL", "truncated": True},
        ]}

    monkeypatch.setattr(server, "_research", fake_research)
    r = await server.research("q", highlights=False, max_chars_per_source=3)
    assert "_full_content" not in r["sources"][0]


async def test_read_surfaces_blocked_by_antibot(app_state, monkeypatch):
    """read() surfaces an anti-bot block as its own code (via structured .code, not a
    message substring), matching scrape/screenshot."""
    from argus.fetch.static import FetchError

    async def blocked_fetch(*a, **k):
        raise FetchError("blocked_by_antibot", "status 403 (anti-bot block)")

    monkeypatch.setattr(server, "fetch", blocked_fetch)
    r = await server.read(f"{BASE}/antibot-page")
    assert r["code"] == "blocked_by_antibot"


async def test_batch_read_reports_antibot_as_failure(app_state, monkeypatch):
    """A batched anti-bot block counts as ok=False (not a KeyError on missing content)."""
    from argus.fetch.static import FetchError

    async def blocked_fetch(*a, **k):
        raise FetchError("blocked_by_antibot", "status 429 (anti-bot block)")

    monkeypatch.setattr(server, "fetch", blocked_fetch)
    out = await server.batch_read([f"{BASE}/a", f"{BASE}/b"])
    assert out["succeeded"] == 0
    assert all(not r["ok"] and r["error"]["code"] == "blocked_by_antibot" for r in out["results"])
