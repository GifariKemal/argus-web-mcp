"""Tests for the SearXNG-backed `search` tool (fully offline via respx)."""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

import argus.search
from argus.search import SearchError, rerank, search

BASE = "http://127.0.0.1:8888"


@pytest.fixture(autouse=True)
def semantic_off(monkeypatch):
    """Force semantic rerank OFF for every test by default.

    The real env HAS fastembed installed, so ``semantic.available()`` returns True
    and would auto-enable the hybrid blend - perturbing the deterministic LEXICAL
    assertions the existing ~50 tests make. Pinning ``available -> False`` keeps the
    lexical-only path exact AND guarantees no test ever loads the real ONNX model.
    The few hybrid tests below re-enable it explicitly via their own monkeypatch.
    """
    monkeypatch.setattr(argus.search.semantic, "available", lambda: False)


def _result(i, engine="duckduckgo", url=None, published=None):
    r = {
        "title": f"Result {i}",
        "url": url or f"https://example.com/{i}",
        "content": f"snippet body {i}",
        "engine": engine,
    }
    if published is not None:
        r["publishedDate"] = published
    return r


def _page(results):
    return {"results": results}


def _query_of(request):
    return parse_qs(urlsplit(str(request.url)).query)


# --------------------------------------------------------------------------- #
# 1. single page
# --------------------------------------------------------------------------- #
@respx.mock
async def test_single_page_maps_shape():
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200, json=_page([_result(1), _result(2, engine="brave"), _result(3)])
        )
    )

    out = await search("python asyncio", count=10, base_url=BASE)

    assert out["query"] == "python asyncio"
    assert out["count"] == 3
    assert len(out["results"]) == 3
    first = out["results"][0]
    assert first == {
        "title": "Result 1",
        "url": "https://example.com/1",
        "snippet": "snippet body 1",
        "engine": "duckduckgo",
    }
    assert out["engines_used"] == ["brave", "duckduckgo"]


@respx.mock
async def test_published_included_when_present():
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200, json=_page([_result(1, published="2026-06-01T00:00:00")])
        )
    )
    out = await search("news", base_url=BASE)
    assert out["results"][0]["published"] == "2026-06-01T00:00:00"


# --------------------------------------------------------------------------- #
# 2. pagination + dedup
# --------------------------------------------------------------------------- #
@respx.mock
async def test_pagination_and_dedup():
    page1 = [_result(i) for i in range(20)]
    # page2: 20 results, urls 15..34 -> 5 overlap (15..19)
    page2 = [_result(i, url=f"https://example.com/{i}") for i in range(15, 35)]

    route = respx.get(f"{BASE}/search")

    def responder(request):
        pageno = int(_query_of(request).get("pageno", ["1"])[0])
        return httpx.Response(200, json=_page(page1 if pageno == 1 else page2))

    route.side_effect = responder

    out = await search("q", count=30, base_url=BASE)

    assert route.call_count == 2
    urls = [r["url"] for r in out["results"]]
    assert len(urls) == len(set(urls))  # deduped
    assert out["count"] == 30
    assert len(out["results"]) == 30
    # order preserved: page1 first
    assert urls[0] == "https://example.com/0"


# --------------------------------------------------------------------------- #
# 3. count respected on page 1
# --------------------------------------------------------------------------- #
@respx.mock
async def test_count_truncates_single_page():
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_page([_result(i) for i in range(20)]))
    )
    out = await search("q", count=5, base_url=BASE)
    assert route.call_count == 1
    assert out["count"] == 5
    assert len(out["results"]) == 5


# --------------------------------------------------------------------------- #
# 4. backend down
# --------------------------------------------------------------------------- #
@respx.mock
async def test_backend_connect_error():
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SearchError) as exc:
        await search("q", base_url=BASE)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_backend_502():
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(502, text="bad gw"))
    with pytest.raises(SearchError) as exc:
        await search("q", base_url=BASE)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_backend_timeout():
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(SearchError) as exc:
        await search("q", base_url=BASE)
    assert exc.value.code == "search_backend_down"


# --------------------------------------------------------------------------- #
# 4b. SearXNG-fallback (SPOF mitigation): primary down -> secondary instance
# --------------------------------------------------------------------------- #
# A public IP literal: validate_url passes it (not a blocked IP) WITHOUT DNS, so the
# fallback path runs fully offline. A plain fallback_client lets respx intercept it.
_FB = "http://93.184.216.34:8888"


@respx.mock
async def test_primary_success_reports_backend_not_degraded():
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_page([_result(1)])))
    out = await search("q", base_url=BASE)
    assert out["degraded"] is False
    assert out["backend"] == BASE


@respx.mock
async def test_falls_back_to_secondary_when_primary_down():
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{_FB}/search").mock(
        return_value=httpx.Response(200, json=_page([_result(1), _result(2)]))
    )
    out = await search(
        "q", base_url=BASE, fallback_base_urls=[_FB], fallback_client=httpx.AsyncClient()
    )
    assert out["count"] == 2
    assert out["degraded"] is True
    assert out["backend"] == _FB


@respx.mock
async def test_no_fallback_configured_raises_unchanged(monkeypatch):
    monkeypatch.delenv("ARGUS_SEARXNG_FALLBACKS", raising=False)
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SearchError) as exc:
        await search("q", base_url=BASE)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_genuine_no_results_does_not_fall_back():
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_page([])))
    fb = respx.get(f"{_FB}/search").mock(
        return_value=httpx.Response(200, json=_page([_result(1)]))
    )
    with pytest.raises(SearchError) as exc:
        await search(
            "q", base_url=BASE, fallback_base_urls=[_FB], fallback_client=httpx.AsyncClient()
        )
    assert exc.value.code == "no_results"
    assert fb.call_count == 0  # backend worked + empty -> never try fallback


@respx.mock
async def test_fallback_list_read_from_env(monkeypatch):
    monkeypatch.setenv("ARGUS_SEARXNG_FALLBACKS", _FB)
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{_FB}/search").mock(
        return_value=httpx.Response(200, json=_page([_result(1)]))
    )
    out = await search("q", base_url=BASE, fallback_client=httpx.AsyncClient())
    assert out["degraded"] is True
    assert out["backend"] == _FB


@respx.mock
async def test_fallback_refused_by_ssrf_guard_is_skipped():
    # Defense-in-depth: in production the fallback runs on the SSRF-guarded client, whose
    # _SafeTransport raises SSRFError before sending if the configured fallback resolves to
    # a private/metadata IP. Here we simulate that refusal; the code must catch it and,
    # with no other fallback, surface the original backend-down error (never crash).
    from argus.security.ssrf import SSRFError as _SSRFError

    bad = "http://169.254.169.254:8888"
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{bad}/search").mock(side_effect=_SSRFError("blocked IP"))
    with pytest.raises(SearchError) as exc:
        await search(
            "q", base_url=BASE,
            fallback_base_urls=[bad],
            fallback_client=httpx.AsyncClient(),
        )
    assert exc.value.code == "search_backend_down"


# --------------------------------------------------------------------------- #
# 5. empty
# --------------------------------------------------------------------------- #
@respx.mock
async def test_no_results():
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_page([])))
    with pytest.raises(SearchError) as exc:
        await search("q", base_url=BASE)
    assert exc.value.code == "no_results"


@respx.mock
async def test_empty_with_unresponsive_engines_is_transient():
    # 0 results because every engine was rate-limited/suspended -> retryable backend issue,
    # NOT "nothing exists". (Reproduces the live 2026-06-24 brave/google throttle.)
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [["brave", "Suspended: too many requests"],
                                         ["google", "Suspended: CAPTCHA"]],
            },
        )
    )
    with pytest.raises(SearchError) as exc:
        await search("q", base_url=BASE)
    assert exc.value.code == "search_backend_down"


# --------------------------------------------------------------------------- #
# 6. params
# --------------------------------------------------------------------------- #
@respx.mock
async def test_params_present():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search(
        "q", category="news", time_range="week", lang="en", base_url=BASE
    )

    assert captured["format"] == ["json"]
    assert captured["categories"] == ["news"]
    assert captured["time_range"] == ["week"]
    assert captured["language"] == ["en"]


@respx.mock
async def test_default_language_param_present_when_unset():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", base_url=BASE)

    assert "time_range" not in captured
    assert captured["language"] == ["en"]
    assert captured["categories"] == ["general"]


@respx.mock
async def test_default_language_env_can_disable(monkeypatch):
    monkeypatch.setenv("ARGUS_DEFAULT_SEARCH_LANG", "")
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", base_url=BASE)

    assert "language" not in captured


@respx.mock
async def test_default_language_env_override(monkeypatch):
    monkeypatch.setenv("ARGUS_DEFAULT_SEARCH_LANG", "id")
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", base_url=BASE)

    assert captured["language"] == ["id"]


@respx.mock
async def test_list_query_joined():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    out = await search(["foo", "bar"], base_url=BASE)
    assert captured["q"] == ["foo bar"]
    # original list preserved in the echoed query
    assert out["query"] == ["foo", "bar"]


@respx.mock
async def test_stops_when_page_adds_nothing():
    # page1: 5 results; page2 returns same 5 (no new urls) -> stop, no infinite loop
    page = [_result(i) for i in range(5)]
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_page(page))
    )
    out = await search("q", count=30, base_url=BASE)
    # asked for 30 but only 5 unique exist; should stop early (<=5 pages cap)
    assert out["count"] == 5
    assert route.call_count <= 5


@respx.mock
async def test_page_cap_at_five():
    # every page returns fresh urls but few results -> never reaches count;
    # must stop at the 5-page cap rather than loop forever.
    counter = {"n": 0}

    def responder(request):
        base = counter["n"] * 3
        counter["n"] += 1
        return httpx.Response(
            200,
            json=_page([_result(base + j, url=f"https://ex.com/{base + j}") for j in range(3)]),
        )

    route = respx.get(f"{BASE}/search")
    route.side_effect = responder

    out = await search("q", count=100, base_url=BASE)
    assert route.call_count == 5
    assert out["count"] == 15  # 5 pages * 3 unique


@respx.mock
async def test_injected_client_not_closed():
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_page([_result(1)]))
    )
    client = httpx.AsyncClient()
    await search("q", base_url=BASE, client=client)
    assert not client.is_closed
    await client.aclose()


# --------------------------------------------------------------------------- #
# 7. rerank - deterministic relevance + dedup
# --------------------------------------------------------------------------- #
def _mapped(title, url, snippet="", engine="duckduckgo"):
    return {"title": title, "url": url, "snippet": snippet, "engine": engine}


def test_rerank_drops_off_topic_and_ranks_relevant_above():
    # The live ESP-Claw failure: 2 "ESP Guitar Company" results polluting the top.
    results = [
        _mapped("Electric Guitar Company - Official", "https://espguitars.com",
                "Premium electric guitars and basses for musicians."),
        _mapped("esp-claw on GitHub", "https://github.com/x/esp-claw",
                "ESP-Claw firmware repository."),
        _mapped("ESP-Claw Documentation", "https://docs.example.com/esp-claw",
                "Docs for the ESP-Claw project."),
        _mapped("Getting started with ESP-Claw", "https://docs.example.com/esp-claw/start",
                "Install and configure ESP-Claw."),
    ]
    out = rerank("ESP-Claw", results)
    urls = [r["url"] for r in out]
    # guitar result has zero query-token overlap -> dropped (>=3 relevant clears the floor)
    assert "https://espguitars.com" not in urls
    # esp-claw results kept and ranked above
    assert "https://github.com/x/esp-claw" in urls
    assert "https://docs.example.com/esp-claw" in urls
    assert urls[0] != "https://espguitars.com"


def test_rerank_hyphen_tokenization():
    # 'esp-claw' must split into {esp, claw} and match both a hyphenated and a
    # space-separated rendering of the same term.
    results = [
        _mapped("Totally unrelated topic", "https://u.example.com", "nothing here"),
        _mapped("ESP-Claw reference", "https://a.example.com", "x"),
        _mapped("The esp claw guide", "https://b.example.com", "y"),
        _mapped("esp-claw notes", "https://c.example.com", "z"),
    ]
    out = rerank("esp-claw", results)
    urls = [r["url"] for r in out]
    # both hyphenated and space-separated renderings match (>=3 relevant -> floor clears)
    assert "https://a.example.com" in urls
    assert "https://b.example.com" in urls
    assert "https://c.example.com" in urls
    assert "https://u.example.com" not in urls  # zero overlap dropped


def test_rerank_title_weighted_above_snippet():
    # Both tokens present, but one has them in the title (stronger) vs only snippet.
    title_hit = _mapped("ESP Claw board", "https://t.example.com", "irrelevant body")
    snippet_hit = _mapped("Random page", "https://s.example.com", "about esp claw stuff")
    out = rerank("esp claw", [snippet_hit, title_hit])
    # title hit must rank first despite being passed in second
    assert out[0]["url"] == "https://t.example.com"


def test_rerank_safety_floor_keeps_backend_top_when_all_low_overlap():
    # No result overlaps the query at all -> would all be dropped, but the floor
    # keeps the backend's top results rather than returning empty.
    results = [_mapped(f"Unrelated {i}", f"https://e.example.com/{i}", "nope")
               for i in range(4)]
    out = rerank("zzqqxx", results)
    assert out  # never empty
    # floor keeps the backend's top _MIN_KEEP results in original order
    assert [r["url"] for r in out] == [r["url"] for r in results[:3]]


def test_rerank_safety_floor_returns_empty_only_for_empty_input():
    assert rerank("anything", []) == []


def test_rerank_dedup_by_normalized_url():
    a = _mapped("ESP-Claw A", "https://example.com/esp-claw", "esp claw")
    # same URL modulo scheme + trailing slash + TRACKING query params
    dup = _mapped("ESP-Claw A mirror", "http://example.com/esp-claw/?utm_source=1", "esp claw")
    out = rerank("esp-claw", [a, dup])
    assert len(out) == 1
    assert out[0]["url"] == "https://example.com/esp-claw"  # first kept


def test_rerank_keeps_distinct_query_param_pages():
    """?v= / ?id= key DISTINCT resources - they must not collapse as duplicates."""
    a = _mapped("ESP-Claw demo video", "https://youtube.com/watch?v=AAA", "esp claw demo")
    b = _mapped("ESP-Claw teardown video", "https://youtube.com/watch?v=BBB", "esp claw teardown")
    out = rerank("esp claw", [a, b])
    assert len(out) == 2


def test_norm_url_tracking_params_and_order():
    from argus.search import _norm_url

    # tracking params stripped, meaningful ones kept sorted (order-insensitive dedup)
    assert _norm_url("https://a.com/p?utm_campaign=x&id=7") == _norm_url(
        "http://a.com/p/?id=7&fbclid=abc"
    )
    # param VALUES stay case-sensitive
    assert _norm_url("https://a.com/w?v=AAA") != _norm_url("https://a.com/w?v=aaa")


def test_rerank_dedup_by_normalized_title():
    a = _mapped("ESP-Claw Project", "https://one.example.com", "esp claw")
    dup = _mapped("  esp-claw   project  ", "https://two.example.com", "esp claw")
    out = rerank("esp-claw", [a, dup])
    assert len(out) == 1
    assert out[0]["url"] == "https://one.example.com"


def test_rerank_handles_missing_url_and_title():
    # A result with neither url nor title must not crash dedup or scoring.
    results = [
        _mapped("ESP-Claw one", "https://1.example.com", "esp claw"),
        {"engine": "x", "snippet": "esp claw mention"},  # no url, no title
        _mapped("ESP-Claw two", "https://2.example.com", "esp claw"),
    ]
    out = rerank("esp-claw", results)
    assert all("title" in r or "snippet" in r for r in out)
    assert any(r.get("url") == "https://1.example.com" for r in out)


def test_rerank_stable_order_on_tie():
    # Identical score (same single token in title) -> original order preserved.
    r1 = _mapped("esp one", "https://1.example.com", "")
    r2 = _mapped("esp two", "https://2.example.com", "")
    r3 = _mapped("esp three", "https://3.example.com", "")
    out = rerank("esp", [r1, r2, r3])
    assert [r["url"] for r in out] == [
        "https://1.example.com",
        "https://2.example.com",
        "https://3.example.com",
    ]


# --------------------------------------------------------------------------- #
# 8. search() integration - rerank demotes/drops off-topic mixed-in result
# --------------------------------------------------------------------------- #
@respx.mock
async def test_search_demotes_off_topic_result_end_to_end():
    # SearXNG fused engines and floated a genuinely off-topic guitar page into the
    # top; rerank both demotes the weak partial match and drops the zero-overlap one.
    page = [
        # zero query-token overlap -> dropped (floor still satisfied by the 4 below)
        {"title": "Electric Guitar Company", "url": "https://espguitars.com",
         "content": "Premium electric guitars and basses.", "engine": "brave"},
        # snippet-only weak match -> demoted below the full-title matches
        {"title": "Random blog", "url": "https://blog.example.com",
         "content": "a passing mention of esp claw somewhere", "engine": "brave"},
        {"title": "esp-claw GitHub", "url": "https://github.com/x/esp-claw",
         "content": "The ESP-Claw firmware.", "engine": "duckduckgo"},
        {"title": "ESP-Claw docs", "url": "https://docs.example.com/esp-claw",
         "content": "ESP-Claw project documentation.", "engine": "duckduckgo"},
        {"title": "ESP-Claw releases", "url": "https://github.com/x/esp-claw/releases",
         "content": "Download ESP-Claw builds.", "engine": "google"},
    ]
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_page(page)))

    out = await search("ESP-Claw", count=8, base_url=BASE)
    urls = [r["url"] for r in out["results"]]

    assert "https://espguitars.com" not in urls  # off-topic, zero overlap -> dropped
    # the weak snippet-only match is demoted to the bottom (not in the top 3)
    assert "https://blog.example.com" == urls[-1]
    # full ESP-Claw matches rank first
    assert urls[0] in {
        "https://github.com/x/esp-claw",
        "https://docs.example.com/esp-claw",
        "https://github.com/x/esp-claw/releases",
    }
    # engines_used derived from the FINAL (reranked) results - brave's guitar is gone,
    # but brave's blog (weak match) survives, plus duckduckgo + google.
    assert out["engines_used"] == ["brave", "duckduckgo", "google"]


# --------------------------------------------------------------------------- #
# 9. auto-pace / backoff - retry transient throttle, not genuine no_results
# --------------------------------------------------------------------------- #
def _throttled():
    return {
        "results": [],
        "unresponsive_engines": [["brave", "Suspended: too many requests"]],
    }


@respx.mock
async def test_retries_then_raises_on_persistent_throttle(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("argus.search.asyncio.sleep", sleep)
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_throttled())
    )

    with pytest.raises(SearchError) as exc:
        await search("q", base_url=BASE, retries=2)

    assert exc.value.code == "search_backend_down"
    # initial attempt + 2 retries = 3 full search attempts
    assert route.call_count == 3
    # slept once before each of the 2 retries, with exponential backoff
    assert sleep.await_count == 2
    assert [c.args[0] for c in sleep.await_args_list] == [0.5, 1.0]


@respx.mock
async def test_retry_succeeds_on_second_attempt(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("argus.search.asyncio.sleep", sleep)

    attempts = {"n": 0}

    def responder(request):
        # Count search ATTEMPTS by page-1 requests (pageno absent or == 1).
        if int(_query_of(request).get("pageno", ["1"])[0]) == 1:
            attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(200, json=_throttled())
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    out = await search("q", base_url=BASE, retries=2)
    assert out["count"] == 1
    assert attempts["n"] == 2  # throttled once, succeeded on the 2nd attempt then stopped
    assert sleep.await_count == 1  # slept once before the single retry
    assert sleep.await_args_list[0].args[0] == 0.5


@respx.mock
async def test_genuine_no_results_does_not_retry(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("argus.search.asyncio.sleep", sleep)
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_page([]))
    )

    with pytest.raises(SearchError) as exc:
        await search("q", base_url=BASE, retries=2)

    assert exc.value.code == "no_results"
    assert route.call_count == 1  # no retry for genuine emptiness
    assert sleep.await_count == 0


@respx.mock
async def test_retries_default_is_two(monkeypatch):
    # Backward-compat: caller need not pass `retries`; default retries transient throttle.
    sleep = AsyncMock()
    monkeypatch.setattr("argus.search.asyncio.sleep", sleep)
    route = respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_throttled())
    )
    with pytest.raises(SearchError):
        await search("q", base_url=BASE)
    assert route.call_count == 3  # 1 + default 2 retries


# --------------------------------------------------------------------------- #
# 10. multi-engine redundancy - spread load across engines for `general`
# --------------------------------------------------------------------------- #
@respx.mock
async def test_general_query_fans_out_to_default_engines():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", base_url=BASE)  # category defaults to general

    assert captured["engines"] == ["duckduckgo,bing,brave,mojeek,startpage,qwant"]
    # general fan-out uses engines, not a forced categories filter expectation
    assert captured["categories"] == ["general"]


@respx.mock
async def test_news_category_keeps_categories_no_forced_engines():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", category="news", base_url=BASE)

    assert captured["categories"] == ["news"]
    assert "engines" not in captured


@respx.mock
async def test_explicit_engines_honored():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", engines=["bing"], base_url=BASE)
    assert captured["engines"] == ["bing"]


@respx.mock
async def test_explicit_engines_precedence_over_general_default():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    # general + explicit engines -> explicit wins (not the default list)
    await search("q", category="general", engines=["bing", "brave"], base_url=BASE)
    assert captured["engines"] == ["bing,brave"]


@respx.mock
async def test_explicit_engines_on_news_category():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", category="news", engines=["bing"], base_url=BASE)
    # explicit engines apply regardless of category
    assert captured["engines"] == ["bing"]
    assert captured["categories"] == ["news"]


# --------------------------------------------------------------------------- #
# 11. rerank v2 - recency tiebreak (does NOT override relevance)
# --------------------------------------------------------------------------- #
def test_rerank_recency_breaks_ties_only():
    # Equal score (both titles cover the full query equally, distinct wording so the
    # title/url dedup doesn't fire); the one carrying a `published` date sorts first.
    no_date = _mapped("esp claw alpha", "https://nodate.example.com", "x")
    dated = _mapped("esp claw beta", "https://dated.example.com", "x")
    dated["published"] = "2026-06-01T00:00:00"
    # passed no-date first, but the dated one must win the tie
    out = rerank("esp claw", [no_date, dated])
    assert out[0]["url"] == "https://dated.example.com"
    assert out[1]["url"] == "https://nodate.example.com"


def test_rerank_relevance_dominates_recency():
    # A stale but more-relevant result must outrank a fresh but less-relevant one.
    stale_relevant = _mapped("esp claw board guide", "https://relevant.example.com",
                             "esp claw")
    fresh_weak = _mapped("Random page", "https://weak.example.com",
                         "a mention of esp somewhere")
    fresh_weak["published"] = "2026-06-24T00:00:00"
    out = rerank("esp claw", [fresh_weak, stale_relevant])
    assert out[0]["url"] == "https://relevant.example.com"  # relevance > recency


# --------------------------------------------------------------------------- #
# 12. domain filters (include / exclude) - competitor parity
# --------------------------------------------------------------------------- #
def _dom_page():
    # Three distinct hosts, all relevant to the query so rerank keeps them.
    return _page([
        {"title": "esp claw github", "url": "https://github.com/x/esp-claw",
         "content": "esp claw repo", "engine": "duckduckgo"},
        {"title": "esp claw medium", "url": "https://medium.com/@a/esp-claw",
         "content": "esp claw article", "engine": "brave"},
        {"title": "esp claw pages", "url": "https://user.github.io/esp-claw",
         "content": "esp claw docs", "engine": "bing"},
    ])


@respx.mock
async def test_include_domains_keeps_only_matching_host():
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_dom_page()))
    out = await search("esp claw", base_url=BASE, include_domains=["github.com"])
    hosts = {urlsplit(r["url"]).netloc for r in out["results"]}
    assert hosts == {"github.com"}
    # github.io must NOT match github.com (suffix is on label boundary)
    assert "user.github.io" not in hosts


@respx.mock
async def test_include_domains_matches_www_variant():
    page = _page([
        {"title": "esp claw www", "url": "https://www.example.com/esp-claw",
         "content": "esp claw", "engine": "duckduckgo"},
        {"title": "esp claw other", "url": "https://other.org/esp-claw",
         "content": "esp claw", "engine": "brave"},
    ])
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page))
    out = await search("esp claw", base_url=BASE, include_domains=["example.com"])
    hosts = {urlsplit(r["url"]).netloc for r in out["results"]}
    assert hosts == {"www.example.com"}  # suffix match: example.com matches www.example.com


@respx.mock
async def test_exclude_domains_drops_matching_host():
    page = _page([
        {"title": "esp claw a", "url": "https://github.com/x/esp-claw",
         "content": "esp claw", "engine": "duckduckgo"},
        {"title": "esp claw pin", "url": "https://www.pinterest.com/esp-claw",
         "content": "esp claw", "engine": "brave"},
        {"title": "esp claw b", "url": "https://docs.example.com/esp-claw",
         "content": "esp claw", "engine": "bing"},
    ])
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page))
    out = await search("esp claw", base_url=BASE, exclude_domains=["pinterest.com"])
    hosts = {urlsplit(r["url"]).netloc for r in out["results"]}
    assert "www.pinterest.com" not in hosts
    assert hosts == {"github.com", "docs.example.com"}


@respx.mock
async def test_include_domains_leaving_nothing_is_no_results():
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_dom_page()))
    with pytest.raises(SearchError) as exc:
        await search("esp claw", base_url=BASE, include_domains=["nonexistent.example"])
    assert exc.value.code == "no_results"


@respx.mock
async def test_include_domains_drops_hostless_and_empty_domain_noop():
    # A result whose URL has no host can never match an include domain (dropped);
    # an empty-string domain in the list never matches anything either.
    page = _page([
        {"title": "esp claw rel", "url": "https://github.com/x/esp-claw",
         "content": "esp claw", "engine": "duckduckgo"},
        {"title": "esp claw hostless", "url": "esp-claw-notes",
         "content": "esp claw", "engine": "brave"},
    ])
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page))
    out = await search("esp claw", base_url=BASE, include_domains=["", "github.com"])
    hosts = {urlsplit(r["url"]).netloc for r in out["results"]}
    assert hosts == {"github.com"}  # hostless dropped, empty domain matched nothing


@respx.mock
async def test_domain_filters_combine_include_and_exclude():
    # include example.com, then exclude a subdomain of it
    page = _page([
        {"title": "esp claw keep", "url": "https://docs.example.com/esp-claw",
         "content": "esp claw", "engine": "duckduckgo"},
        {"title": "esp claw drop", "url": "https://ads.example.com/esp-claw",
         "content": "esp claw", "engine": "brave"},
        {"title": "esp claw off", "url": "https://other.org/esp-claw",
         "content": "esp claw", "engine": "bing"},
    ])
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page))
    out = await search("esp claw", base_url=BASE,
                       include_domains=["example.com"], exclude_domains=["ads.example.com"])
    hosts = {urlsplit(r["url"]).netloc for r in out["results"]}
    assert hosts == {"docs.example.com"}


# --------------------------------------------------------------------------- #
# 13. safesearch - passed through to SearXNG params
# --------------------------------------------------------------------------- #
@respx.mock
async def test_safesearch_param_present_when_set():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", safesearch=2, base_url=BASE)
    assert captured["safesearch"] == ["2"]


@respx.mock
async def test_safesearch_absent_when_zero_default():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", base_url=BASE)  # default safesearch=0
    assert "safesearch" not in captured


# --------------------------------------------------------------------------- #
# 14. rerank recency BOOST (v2) - additive, bounded, relevance still dominates
# --------------------------------------------------------------------------- #
def test_rerank_recency_boost_stronger_than_tiebreak():
    # Equal base relevance: with recency=True, the dated result must rank first AND
    # the gap is a real score boost (not just the published-tiebreak). We prove it by
    # giving the no-date result a STRICTLY HIGHER base relevance that the tiebreak
    # alone could never overcome, yet the boost flips it.
    # no_date: full query in title (score 2.0). dated: query in title too (2.0) BUT we
    # make no_date marginally stronger via snippet so tiebreak alone can't reorder it.
    stronger_nodate = _mapped("esp claw guide", "https://nodate.example.com", "esp claw")
    weaker_dated = _mapped("esp claw intro", "https://dated.example.com", "esp")
    weaker_dated["published"] = "2026-06-24T00:00:00"
    # Without recency: nodate (snippet fully covers too) outranks dated.
    base = rerank("esp claw", [weaker_dated, stronger_nodate], recency=False)
    assert base[0]["url"] == "https://nodate.example.com"
    # With recency: the +boost lifts the dated one above the slightly-stronger nodate.
    boosted = rerank("esp claw", [weaker_dated, stronger_nodate], recency=True)
    assert boosted[0]["url"] == "https://dated.example.com"


def test_rerank_recency_boost_does_not_override_relevance():
    # A fully-irrelevant fresh result must NOT outrank a relevant stale one, even with
    # the boost (boost is a fraction of the title weight). The irrelevant one is dropped
    # entirely (zero overlap), and even if kept by floor it ranks below.
    fresh_irrelevant = _mapped("Guitar shop", "https://guitar.example.com",
                               "premium electric guitars")
    fresh_irrelevant["published"] = "2026-06-24T00:00:00"
    stale_relevant = _mapped("esp claw board", "https://relevant.example.com", "esp claw")
    filler = [_mapped(f"esp claw doc {i}", f"https://d{i}.example.com", "esp claw")
              for i in range(2)]
    out = rerank("esp claw", [fresh_irrelevant, stale_relevant, *filler], recency=True)
    assert out[0]["url"] != "https://guitar.example.com"  # relevance dominates the boost
    assert out[0]["url"] in {
        "https://relevant.example.com",
        "https://d0.example.com",
        "https://d1.example.com",
    }


def test_rerank_recency_default_false_keeps_general_behavior():
    # Backward-compat: default recency=False -> published is only a tiebreak, not a boost.
    stronger_nodate = _mapped("esp claw guide", "https://nodate.example.com", "esp claw")
    weaker_dated = _mapped("esp claw intro", "https://dated.example.com", "esp")
    weaker_dated["published"] = "2026-06-24T00:00:00"
    out = rerank("esp claw", [weaker_dated, stronger_nodate])  # default
    assert out[0]["url"] == "https://nodate.example.com"


@respx.mock
async def test_search_news_passes_recency_to_rerank():
    # news category => recency=True. A fresh, equally/slightly-weaker-relevant result
    # should surface above a stronger no-date one due to the boost (proves wiring).
    page = _page([
        {"title": "esp claw report", "url": "https://nodate.example.com",
         "content": "esp claw"},  # stronger base (snippet covers), no date
        {"title": "esp claw update", "url": "https://dated.example.com",
         "content": "esp", "publishedDate": "2026-06-24T00:00:00"},  # weaker base, dated
    ])
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page))
    out = await search("esp claw", category="news", base_url=BASE)
    assert out["results"][0]["url"] == "https://dated.example.com"


@respx.mock
async def test_search_time_range_passes_recency_to_rerank():
    page = _page([
        {"title": "esp claw report", "url": "https://nodate.example.com",
         "content": "esp claw"},
        {"title": "esp claw update", "url": "https://dated.example.com",
         "content": "esp", "publishedDate": "2026-06-24T00:00:00"},
    ])
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page))
    out = await search("esp claw", time_range="week", base_url=BASE)
    assert out["results"][0]["url"] == "https://dated.example.com"


@respx.mock
async def test_search_general_does_not_apply_recency_boost():
    # general (no time_range) => recency=False; stronger no-date stays on top.
    page = _page([
        {"title": "esp claw report", "url": "https://nodate.example.com",
         "content": "esp claw"},
        {"title": "esp claw update", "url": "https://dated.example.com",
         "content": "esp", "publishedDate": "2026-06-24T00:00:00"},
    ])
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page))
    out = await search("esp claw", base_url=BASE)
    assert out["results"][0]["url"] == "https://nodate.example.com"


# --------------------------------------------------------------------------- #
# 15. HYBRID semantic rerank - auto-enabled when semantic.available(), offline
# --------------------------------------------------------------------------- #
def _enable_semantic(monkeypatch, sim_map):
    """Turn semantic ON and stub similarities() deterministically (no model load).

    ``sim_map`` maps a substring of the ``title + " " + snippet`` doc string to a
    cosine value; the stub returns the value for the first matching key (default 0.0).
    """
    monkeypatch.setattr(argus.search.semantic, "available", lambda: True)

    def _sims(query, docs):
        out = []
        for d in docs:
            val = 0.0
            for key, v in sim_map.items():
                if key in d:
                    val = v
                    break
            out.append(val)
        return out

    monkeypatch.setattr(argus.search.semantic, "similarities", _sims)


def test_semantic_rescues_zero_lexical_overlap_paraphrase(monkeypatch):
    # A conceptual paraphrase with ZERO lexical overlap but HIGH semantic similarity must
    # be KEPT (not hard-dropped) and rank ABOVE a weak partial-lexical low-sim result. This
    # is the benchmark's how-to / conceptual weak spot the hybrid blend fixes.
    # query = "kill signal":
    #  rescue : lex=0 (no shared token), sem=0.90 -> blended 0.6*0.90 + 0.4*0   = 0.540
    #  weak   : lex covers 1/2 tokens in snippet (0.5), sem=0.40
    #  low    : lex=0, sem=0.10 (< _SEM_FLOOR 0.3) -> DROPPED
    #  filler : keep >= _MIN_KEEP after dropping `low`
    # All four docs have ZERO lexical overlap with "kill signal" (so lex_norm is 0 for
    # every kept row and the blend is driven purely by semantics - isolating the rescue):
    rescue = _mapped("Stop a process from terminating", "https://rescue.example.com",
                     "graceful shutdown handling")     # sem 0.90 -> kept, ranks top
    midA = _mapped("Process lifecycle notes", "https://mida.example.com",
                   "starting and stopping daemons")    # sem 0.50 -> kept, below rescue
    midB = _mapped("Daemon supervision", "https://midb.example.com",
                   "supervisor restarts workers")       # sem 0.45 -> kept
    low = _mapped("Unrelated cooking blog", "https://low.example.com",
                  "best pasta recipes ever")            # sem 0.10 (< floor) -> dropped
    _enable_semantic(monkeypatch, {
        "Stop a process": 0.90,
        "Process lifecycle": 0.50,
        "Daemon supervision": 0.45,
        "cooking": 0.10,    # zero lexical + sim < _SEM_FLOOR -> dropped
    })
    out = rerank("kill signal", [low, midB, rescue, midA])
    urls = [r["url"] for r in out]
    assert "https://rescue.example.com" in urls   # paraphrase rescued despite zero overlap
    assert "https://low.example.com" not in urls  # zero-lex + low-sim (<0.3) dropped
    assert urls[0] == "https://rescue.example.com"  # highest sim ranks top among rescues


def test_semantic_zero_lexical_low_sim_dropped(monkeypatch):
    # Zero lexical overlap AND semantic sim < _SEM_FLOOR (0.3) -> clearly irrelevant, dropped
    # (kept above _MIN_KEEP so the floor doesn't force it back in).
    relevant = [_mapped(f"kill signal doc {i}", f"https://d{i}.example.com",
                        "handle a kill signal") for i in range(3)]
    junk = _mapped("Pasta recipes", "https://junk.example.com", "cooking tips")
    _enable_semantic(monkeypatch, {
        "kill signal": 0.7,
        "Pasta": 0.20,  # < 0.3 floor, zero lexical
    })
    out = rerank("kill signal", [*relevant, junk])
    assert "https://junk.example.com" not in [r["url"] for r in out]


def test_semantic_blend_ordering_matches_formula(monkeypatch):
    # Fixed lexical + semantic vectors; assert the blended order is exactly
    # 0.6*sem + 0.4*lex_norm. Three docs all share the single query token "esp" so
    # none is dropped; lexical scores differ via extra title tokens / snippet coverage.
    # Doc A: title "esp" only            -> lex = TITLE_WEIGHT*(1/1)=2.0
    # Doc B: title "esp", snippet "esp"  -> lex = 2.0 + 1.0 = 3.0  (max)
    # Doc C: snippet "esp" only          -> lex = 0 + 1.0 = 1.0
    # lex_norm = lex/3.0 -> A=0.6667, B=1.0, C=0.3333
    a = _mapped("esp alpha", "https://a.example.com", "nothing")
    b = _mapped("esp beta", "https://b.example.com", "esp body")
    c = _mapped("gamma page", "https://c.example.com", "esp body")
    _enable_semantic(monkeypatch, {
        "esp alpha": 0.90,   # A: blended = 0.6*0.90 + 0.4*0.6667 = 0.8067
        "esp beta": 0.10,    # B: blended = 0.6*0.10 + 0.4*1.0    = 0.46
        "gamma page": 0.80,  # C: blended = 0.6*0.80 + 0.4*0.3333 = 0.6133
    })
    # Expected blended order: A (0.807) > C (0.613) > B (0.46)
    out = rerank("esp", [a, b, c])
    assert [r["url"] for r in out] == [
        "https://a.example.com",
        "https://c.example.com",
        "https://b.example.com",
    ]


def test_semantic_off_identical_to_lexical(monkeypatch):
    # available() -> False (the autouse default): hybrid path must be a no-op and the
    # exact lexical behavior holds, INCLUDING zero-overlap drop. Mirrors the canonical
    # lexical test test_rerank_drops_off_topic_and_ranks_relevant_above.
    results = [
        _mapped("Electric Guitar Company - Official", "https://espguitars.com",
                "Premium electric guitars and basses for musicians."),
        _mapped("esp-claw on GitHub", "https://github.com/x/esp-claw",
                "ESP-Claw firmware repository."),
        _mapped("ESP-Claw Documentation", "https://docs.example.com/esp-claw",
                "Docs for the ESP-Claw project."),
        _mapped("Getting started with ESP-Claw", "https://docs.example.com/esp-claw/start",
                "Install and configure ESP-Claw."),
    ]
    # similarities would raise if called (proving the OFF path never touches it).
    def _boom(q, d):
        raise AssertionError("similarities must not be called when semantic is OFF")
    monkeypatch.setattr(argus.search.semantic, "similarities", _boom)
    out = rerank("ESP-Claw", results)
    urls = [r["url"] for r in out]
    assert "https://espguitars.com" not in urls  # zero overlap still dropped
    assert "https://github.com/x/esp-claw" in urls
    assert urls[0] != "https://espguitars.com"


def test_semantic_force_off_overrides_available(monkeypatch):
    # available() True but semantic_rerank=False forces lexical (zero-overlap dropped).
    monkeypatch.setattr(argus.search.semantic, "available", lambda: True)
    def _boom(q, d):
        raise AssertionError("similarities must not be called when forced OFF")
    monkeypatch.setattr(argus.search.semantic, "similarities", _boom)
    results = [
        _mapped("Guitar shop", "https://guitar.example.com", "premium guitars"),
        _mapped("esp claw a", "https://a.example.com", "esp claw"),
        _mapped("esp claw b", "https://b.example.com", "esp claw"),
        _mapped("esp claw c", "https://c.example.com", "esp claw"),
    ]
    out = rerank("esp claw", results, semantic_rerank=False)
    assert "https://guitar.example.com" not in [r["url"] for r in out]


def test_semantic_error_falls_back_to_lexical(monkeypatch):
    # similarities() raising (model error / SemanticUnavailable) must be caught and the
    # lexical rerank used instead - never fail the rerank because of semantic.
    from argus.semantic import SemanticUnavailable

    monkeypatch.setattr(argus.search.semantic, "available", lambda: True)

    def _raise(q, d):
        raise SemanticUnavailable("model blew up")

    monkeypatch.setattr(argus.search.semantic, "similarities", _raise)
    results = [
        _mapped("Guitar shop", "https://guitar.example.com", "premium guitars"),
        _mapped("esp claw a", "https://a.example.com", "esp claw"),
        _mapped("esp claw b", "https://b.example.com", "esp claw"),
        _mapped("esp claw c", "https://c.example.com", "esp claw"),
    ]
    out = rerank("esp claw", results)
    urls = [r["url"] for r in out]
    # lexical fallback: zero-overlap guitar dropped, relevant kept
    assert "https://guitar.example.com" not in urls
    assert "https://a.example.com" in urls


@respx.mock
async def test_search_falls_back_when_semantic_raises(monkeypatch):
    # End-to-end: semantic available but similarities() raises -> search must STILL
    # return (lexical fallback), never propagate the semantic error.
    monkeypatch.setattr(argus.search.semantic, "available", lambda: True)

    def _raise(q, d):
        raise RuntimeError("onnx session died")

    monkeypatch.setattr(argus.search.semantic, "similarities", _raise)
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_page([_result(1), _result(2)]))
    )
    out = await search("python asyncio", base_url=BASE)
    assert out["count"] >= 1


@respx.mock
async def test_search_passes_semantic_auto(monkeypatch):
    # search() auto-enables semantic when available: a zero-lexical-overlap paraphrase
    # is rescued end-to-end (would be DROPPED on the lexical-only path).
    monkeypatch.setattr(argus.search.semantic, "available", lambda: True)

    def _sims(query, docs):
        return [0.9 if "Paraphrase" in d else 0.5 for d in docs]

    monkeypatch.setattr(argus.search.semantic, "similarities", _sims)
    page = _page([
        {"title": "Paraphrase rescue", "url": "https://para.example.com",
         "content": "conceptually related but no shared words", "engine": "duckduckgo"},
        {"title": "zzqq token doc", "url": "https://lex.example.com",
         "content": "zzqq match", "engine": "brave"},
    ])
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page))
    out = await search("zzqq", base_url=BASE)
    urls = [r["url"] for r in out["results"]]
    assert "https://para.example.com" in urls  # rescued by semantic despite zero overlap


# --------------------------------------------------------------------------- #
# 16. relative-relevance gate (_REL_FLOOR) - gentle backfill trimming
# --------------------------------------------------------------------------- #
def test_rel_floor_trims_weak_backfill_on_lexical_path():
    # Mirrors the live failure: 3 strong GitHub results + 2 weak Docker Hub results
    # whose only overlap with "ESP-IDF wifi provisioning manager" is the single generic
    # token "manager" in the snippet.  All five survive the zero-overlap drop (they all
    # have non-zero overlap), but the 2 weak ones have
    #   score = 0.2   (1 snippet token / 5 query tokens)
    # while the strong ones have
    #   score = 3.0   (full title + full snippet coverage)
    # Ratio = 0.2 / 3.0 = 0.067 << _REL_FLOOR (0.25) -> gate trims them.
    # Use distinct titles to avoid title-dedup collapsing the 3 strong results.
    query = "ESP-IDF wifi provisioning manager"
    strong = [
        _mapped(
            f"ESP-IDF Wifi Provisioning Manager doc {i}",
            f"https://github.com/espressif/esp-idf/{i}",
            "esp idf wifi provisioning manager component",
        )
        for i in range(3)
    ]
    weak = [
        _mapped(
            f"cert-manager Docker Hub image {i}",
            f"https://hub.docker.com/r/cert-manager/{i}",
            "kubernetes certificate manager container image",
        )
        for i in range(2)
    ]
    out = rerank(query, strong + weak)
    urls = [r["url"] for r in out]
    # 3 strong results kept
    assert len(out) == 3
    for r in strong:
        assert r["url"] in urls
    # 2 weak backfill trimmed
    for r in weak:
        assert r["url"] not in urls


def test_rel_floor_does_not_fire_on_equal_moderate_overlap():
    # 5 results all with the SAME moderate score (2 of 5 query tokens in title).
    # top_score == every result's score -> ratio == 1.0 for all -> gate never fires.
    query = "ESP-IDF wifi provisioning manager"
    results = [
        _mapped(
            f"ESP-IDF provisioning doc {i}",
            f"https://docs.example.com/{i}",
            "some unrelated body",
        )
        for i in range(5)
    ]
    out = rerank(query, results)
    # All 5 must be kept; no result was trimmed.
    assert len(out) == 5


def test_rel_floor_respects_min_keep_floor():
    # 2 strong + 1 weak (total = 3 = _MIN_KEEP).  The weak result ratio
    # (cert-manager: score=0.60, top=3.0, ratio=0.20 < _REL_FLOOR=0.25) would
    # trigger the gate, but dropping it leaves only 2 < _MIN_KEEP -- so the floor
    # blocks the drop and all 3 are kept.  Use distinct titles to avoid dedup.
    query = "ESP-IDF wifi provisioning manager"
    strong = [
        _mapped(
            "ESP-IDF Wifi Provisioning Manager Guide",
            "https://github.com/espressif/esp-idf/0",
            "esp idf wifi provisioning manager",
        ),
        _mapped(
            "ESP-IDF Provisioning Manager API",
            "https://github.com/espressif/esp-idf/1",
            "esp idf wifi provisioning manager",
        ),
    ]
    weak = _mapped(
        "cert-manager Docker Hub",
        "https://hub.docker.com/r/cert-manager/0",
        "kubernetes certificate manager container",
    )
    out = rerank(query, strong + [weak])
    assert len(out) == 3  # floor prevents drop below _MIN_KEEP
    assert weak["url"] in [r["url"] for r in out]


def test_rel_floor_trims_weak_backfill_on_hybrid_path(monkeypatch):
    # Same scenario as test_rel_floor_trims_weak_backfill_on_lexical_path but with
    # the hybrid semantic path enabled.  Use distinct titles to avoid title-dedup.
    # strong: lex=3.0, lex_norm=1.0, sem=0.90 -> blended=0.6*0.90+0.4*1.0=0.94
    # weak  : lex=0.6, lex_norm=0.6/3.0=0.20, sem=0.10 -> blended=0.6*0.10+0.4*0.20=0.14
    # ratio = 0.14/0.94 = 0.149 < _REL_FLOOR (0.25) -> gate trims the 2 weak results.
    _enable_semantic(monkeypatch, {
        "ESP-IDF Wifi Provisioning Manager": 0.90,
        "cert-manager Docker Hub": 0.10,
    })
    query = "ESP-IDF wifi provisioning manager"
    strong = [
        _mapped(
            f"ESP-IDF Wifi Provisioning Manager doc {i}",
            f"https://github.com/espressif/esp-idf/{i}",
            "esp idf wifi provisioning manager component",
        )
        for i in range(3)
    ]
    weak = [
        _mapped(
            f"cert-manager Docker Hub image {i}",
            f"https://hub.docker.com/r/cert-manager/{i}",
            "kubernetes certificate manager container image",
        )
        for i in range(2)
    ]
    out = rerank(query, strong + weak)
    urls = [r["url"] for r in out]
    assert len(out) == 3
    for r in strong:
        assert r["url"] in urls
    for r in weak:
        assert r["url"] not in urls


# --------------------------------------------------------------------------- #
# 17. Docker Hub host-penalty (F3) - de-prioritise docker.com on non-docker queries
# --------------------------------------------------------------------------- #
def test_dockerhub_penalised_on_non_docker_it_query():
    # F3 scenario: moderate-overlap GitHub results + Docker Hub results sharing only
    # generic tokens ("manager") survive the existing _REL_FLOOR because the ratio
    # (~0.375) is above _REL_FLOOR (0.25).  The host penalty must push them below it.
    #
    # Score maths (6-token query, lexical path):
    #   GitHub  title={esp,idf,provisioning,component,guide}, 4/6 overlap -> lex=2*(4/6)=1.333
    #   Docker  title={cert,manager,controller}, 1/6 -> 2*(1/6);
    #           snip={kubernetes,certificate,manager,image}, 1/6 -> total raw=0.5
    #   ratio_raw = 0.5/1.333 = 0.375 > 0.25 (gate does NOT fire without penalty)
    #   With _DOCKER_PENALTY=0.4: penalised_score=0.5*0.4=0.2; ratio=0.2/1.333=0.15 < 0.25
    #   -> gate fires -> Docker Hub trimmed out of results.
    query = "ESP-IDF wifi provisioning manager component"
    strong = [
        _mapped(
            f"ESP-IDF provisioning component guide {i}",
            f"https://github.com/espressif/esp-idf/{i}",
            "",
        )
        for i in range(3)
    ]
    weak = [
        _mapped(
            "cert-manager-controller",
            f"https://hub.docker.com/r/cert-manager/{i}",
            "kubernetes certificate manager image",
        )
        for i in range(2)
    ]
    out = rerank(query, strong + weak)
    urls = [r["url"] for r in out]
    # GitHub results kept
    for r in strong:
        assert r["url"] in urls
    # Docker Hub results demoted out of the top (penalty pushes them below the rel floor)
    for r in weak:
        assert r["url"] not in urls


def test_dockerhub_not_penalised_on_docker_intent_query():
    # A query with docker-intent tokens ("docker") must NOT apply the penalty:
    # Docker Hub results remain ranked by plain lexical score.
    # query tokens: {docker, image, for, postgres} -> "docker" is a container-intent token
    # -> no penalty applied -> Docker Hub result with good title coverage ranks high.
    query = "docker image for postgres"
    docker_result = _mapped(
        "postgres Docker Hub image",
        "https://hub.docker.com/r/_/postgres",
        "official postgres docker image",
    )
    github_result = _mapped(
        "postgres GitHub repo",
        "https://github.com/postgres/postgres",
        "postgres source code",
    )
    out = rerank(query, [docker_result, github_result])
    urls = [r["url"] for r in out]
    # Docker Hub result must still be present and not demoted to last place
    assert "https://hub.docker.com/r/_/postgres" in urls
    # It should rank first or second (not knocked out) - both are relevant, Docker Hub
    # actually has better title coverage ("docker","image","postgres" vs "postgres" only)
    assert urls[0] == "https://hub.docker.com/r/_/postgres"


def test_dockerhub_penalty_no_regression_without_docker_results():
    # When there are NO hub.docker.com results, the penalty code-path never fires and
    # the output is identical to the pure lexical rerank.  Regression safety check.
    query = "ESP-IDF wifi provisioning manager component"
    results = [
        _mapped(
            f"ESP-IDF provisioning component guide {i}",
            f"https://github.com/espressif/esp-idf/{i}",
            "esp idf wifi provisioning manager component",
        )
        for i in range(4)
    ]
    out_with_penalty_code = rerank(query, results)
    # All 4 should be kept (strong results, no Docker Hub noise)
    assert len(out_with_penalty_code) == 4
    urls = [r["url"] for r in out_with_penalty_code]
    for r in results:
        assert r["url"] in urls


# --------------------------------------------------------------------------- #
# 18. generic-token down-weight - a result matching ONLY a generic/low-info
#     query token ranks below results matching a content-bearing token.
# --------------------------------------------------------------------------- #
def test_generic_only_match_ranks_below_content_match():
    # Query mixes a content token ("kubernetes") with a generic one ("manager").
    # - `content` matches "kubernetes" (content-bearing) -> full score, NO penalty.
    # - `generic` matches only "manager" (generic, low-info) -> down-weighted.
    # Equal RAW token counts (both match exactly 1 query token in the title), so without
    # the generic down-weight they would tie; with it, the content match ranks strictly
    # above the generic-only match.
    query = "kubernetes manager"
    content = _mapped(
        "kubernetes orchestration notes",
        "https://content.example.com",
        "container scheduling",
    )
    generic = _mapped(
        "project manager handbook",
        "https://generic.example.com",
        "task tracking",
    )
    # `generic` passed first so a tie would leave it on top (stable order); the
    # down-weight must flip it below the content match.
    out = rerank(query, [generic, content])
    urls = [r["url"] for r in out]
    assert urls.index("https://content.example.com") < urls.index(
        "https://generic.example.com"
    )


def test_generic_plus_content_match_not_penalised():
    # A result matching a generic token AND a content token must NOT be penalised:
    # it keeps full score and is not demoted relative to a content-only match with the
    # SAME content coverage.  Here both match the content token "kubernetes"; `both`
    # additionally matches the generic "manager".  `both` must not rank below `content`.
    query = "kubernetes manager"
    both = _mapped(
        "kubernetes manager guide",
        "https://both.example.com",
        "orchestration",
    )
    content = _mapped(
        "kubernetes deep dive",
        "https://content.example.com",
        "orchestration",
    )
    # `both` has >= the content coverage of `content` plus an (unpenalised) generic
    # match, so it must rank first (passed first; equal-or-higher score keeps it there).
    out = rerank(query, [both, content])
    assert out[0]["url"] == "https://both.example.com"


def test_generic_down_weight_preserves_zero_overlap_drop_and_min_keep():
    # A purely-generic-only match is DEMOTED, not dropped, while a zero-overlap result is
    # still dropped (overlap gate unchanged) - subject to the _MIN_KEEP floor.
    query = "kubernetes manager"
    content = [
        _mapped(
            f"kubernetes orchestration {i}",
            f"https://k{i}.example.com",
            "scheduling",
        )
        for i in range(3)
    ]
    generic = _mapped(
        "project manager handbook",
        "https://generic.example.com",
        "task tracking",
    )
    zero = _mapped("totally unrelated", "https://zero.example.com", "nothing here")
    out = rerank(query, [*content, generic, zero])
    urls = [r["url"] for r in out]
    # zero-overlap result dropped (>=3 relevant content matches satisfy the floor)
    assert "https://zero.example.com" not in urls
    # generic-only match still present (demoted, not dropped) but ranked last
    assert urls[-1] == "https://generic.example.com"


@respx.mock
async def test_off_topic_results_flagged_low_relevance():
    # Backend returns results (survive the rerank floor) that share NO token with the
    # query - the concurrency-garbage case where SearXNG hands back an unrelated page set.
    # search() must not silently pass it off as clean.
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_page([_result(1), _result(2), _result(3)]))
    )
    out = await search("nous hermes kanban orchestration", base_url=BASE)
    assert out["count"] >= 1  # floor kept results; nothing raised no_results
    assert out["degraded"] is True
    assert out["degraded_reason"] == "low_relevance"


@respx.mock
async def test_on_topic_results_not_flagged():
    # A result whose title/snippet overlaps the query must stay degraded=False.
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            json=_page(
                [
                    {
                        "title": "Hermes agent kanban guide",
                        "url": "https://example.com/hermes",
                        "content": "orchestration for the hermes agent",
                        "engine": "duckduckgo",
                    }
                ]
            ),
        )
    )
    out = await search("nous hermes kanban orchestration", base_url=BASE)
    assert out["degraded"] is False
    assert out["degraded_reason"] is None


@respx.mock
async def test_majority_off_topic_flagged_despite_incidental_match():
    # The observed research-layer case: a throttled sole-engine returns generic filler where
    # ONE result incidentally shares a single query token ("research" in an unrelated MS doc).
    # Majority rule must still flag it - a lone incidental hit no longer masks a garbage set.
    respx.get(f"{BASE}/search").mock(
        return_value=httpx.Response(
            200,
            json=_page(
                [
                    {  # incidental single-token overlap ("research")
                        "title": "Get started with Microsoft 365 Copilot Notebooks",
                        "url": "https://learn.microsoft.com/copilot",
                        "content": "Organize research for a new project with Copilot.",
                        "engine": "bing",
                    },
                    {
                        "title": "Create installation media for Windows",
                        "url": "https://support.microsoft.com/windows-media",
                        "content": "Use a USB stick to install a fresh copy of Windows.",
                        "engine": "bing",
                    },
                    {
                        "title": "How to help keep your Microsoft account secure",
                        "url": "https://support.microsoft.com/account-secure",
                        "content": "Use the Authenticator app to sign in without a password.",
                        "engine": "bing",
                    },
                ]
            ),
        )
    )
    out = await search("nous hermes agent self-improving learning loop skills", base_url=BASE)
    assert out["degraded"] is True  # 1/3 overlap < half -> flagged
    assert out["degraded_reason"] == "low_relevance"


def test_rerank_safety_floor_backfills_around_relevant_tail():
    """A relevant hit at a tail position must survive the safety floor - the old code
    REPLACED the kept set with the backend's first-N junk."""
    junk = [
        _mapped(f"Unrelated filler page {i}", f"https://junk{i}.example.com/{i}", f"filler {i}")
        for i in range(8)
    ]
    relevant = [
        _mapped("ESP-Claw firmware guide", "https://good.example.com/1", "esp claw docs"),
        _mapped("ESP-Claw hardware wiki", "https://good.example.com/2", "esp claw wiki"),
    ]
    out = rerank("esp claw", junk + relevant, semantic_rerank=False)
    urls = [r["url"] for r in out]
    assert "https://good.example.com/1" in urls
    assert "https://good.example.com/2" in urls


def test_relevance_guard_ignores_stopword_overlap():
    """Garbage sharing only stopwords ('to', 'the') with a natural-language query must
    still be flagged degraded."""
    import asyncio

    import httpx

    garbage = [
        {"title": "Get the best deals on laptops", "url": "https://f1.com/a",
         "content": "Use a USB stick to install a fresh copy of Windows.", "engine": "bing"},
        {"title": "How to cook the perfect steak", "url": "https://f2.com/b",
         "content": "A guide to the best searing techniques.", "engine": "bing"},
        {"title": "The 10 best beaches to visit", "url": "https://f3.com/c",
         "content": "Places to see in the summer.", "engine": "bing"},
    ]

    def handler(request):
        return httpx.Response(200, json={"results": garbage, "unresponsive_engines": []})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await search("how to deploy the hermes agent", client=client)

    out = asyncio.get_event_loop().run_until_complete(run()) if False else asyncio.run(run())
    assert out["degraded"] is True
    assert out["degraded_reason"] == "low_relevance"


def test_relevance_guard_on_topic_natural_language_not_flagged():
    import asyncio

    import httpx

    good = [
        {"title": "Deploy the Hermes agent on Ubuntu", "url": "https://d1.com/a",
         "content": "Steps to deploy the hermes agent.", "engine": "ddg"},
        {"title": "Hermes agent deployment guide", "url": "https://d2.com/b",
         "content": "Full deploy walkthrough for hermes.", "engine": "ddg"},
        {"title": "Hermes agent config", "url": "https://d3.com/c",
         "content": "Configuration and deploy tips.", "engine": "ddg"},
    ]

    def handler(request):
        return httpx.Response(200, json={"results": good, "unresponsive_engines": []})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await search("how to deploy the hermes agent", client=client)

    out = asyncio.run(run())
    assert out["degraded"] is False


def test_relevance_guard_ignores_generic_only_overlap():
    import asyncio

    import httpx

    generic = [
        {"title": "Fastest cars in the world", "url": "https://e1.com/a",
         "content": "A ranked list of the fastest vehicles.", "engine": "bing"},
        {"title": "Fastest animals on earth", "url": "https://e2.com/b",
         "content": "Speed records from wildlife.", "engine": "bing"},
        {"title": "Fastest internet speed test", "url": "https://e3.com/c",
         "content": "Measure your connection speed.", "engine": "bing"},
    ]

    def handler(request):
        return httpx.Response(200, json={"results": generic, "unresponsive_engines": []})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await search("fastest way to parse large JSON in python", client=client)

    out = asyncio.run(run())
    assert out["degraded"] is True
    assert out["degraded_reason"] == "low_relevance"


@respx.mock
async def test_low_relevance_general_search_rescues_to_routed_category(monkeypatch):
    monkeypatch.setattr(argus.search.semantic, "available", lambda: False)
    seen_categories = []

    generic = [
        {"title": "Fastest cars in the world", "url": "https://e1.com/a",
         "content": "A ranked list of the fastest vehicles.", "engine": "bing"},
        {"title": "Fastest animals on earth", "url": "https://e2.com/b",
         "content": "Speed records from wildlife.", "engine": "bing"},
        {"title": "Fastest internet speed test", "url": "https://e3.com/c",
         "content": "Measure your connection speed.", "engine": "bing"},
    ]
    good = [
        {"title": "Python parse large JSON efficiently", "url": "https://s1.com/a",
         "content": "Streaming JSON parsing in python.", "engine": "stackoverflow"},
        {"title": "Large JSON parsing in Python", "url": "https://s2.com/b",
         "content": "Use ijson for large files.", "engine": "stackoverflow"},
        {"title": "JSON parser performance Python", "url": "https://s3.com/c",
         "content": "Compare json, orjson, and streaming parse.", "engine": "stackoverflow"},
    ]

    def responder(request):
        category = request.url.params.get("categories")
        seen_categories.append(category)
        page = generic if category == "general" else good
        return httpx.Response(200, json={"results": page, "unresponsive_engines": []})

    respx.get(f"{BASE}/search").side_effect = responder

    out = await search("fastest way to parse large JSON in python", base_url=BASE)

    assert seen_categories[:3] == ["general", "general", "it"]
    assert out["degraded"] is False
    assert out["degraded_reason"] is None
    assert out["rescued_category"] == "it"
    assert "Python" in out["results"][0]["title"]


async def test_category_rescue_preserves_backend_failover_degraded(monkeypatch):
    monkeypatch.setattr(argus.search.semantic, "available", lambda: False)

    async def fake_backend(q, count, params, base_url, client, retries):
        if base_url == BASE:
            raise SearchError("search_backend_down", "primary down")
        if params["categories"] == "general":
            return [
                {
                    "title": "Garden weather",
                    "url": f"https://fallback.example/{i}",
                    "snippet": "flowers and rain",
                    "engine": "fallback",
                }
                for i in range(5)
            ]
        assert params["categories"] == "it"
        return [
            {
                "title": "React hydration mismatch in SSR",
                "url": "https://fallback.example/react",
                "snippet": "debug hydration mismatch in server rendered React",
                "engine": "fallback",
            }
        ]

    monkeypatch.setattr(argus.search, "_search_backend", fake_backend)

    out = await search(
        "React hydration mismatch SSR",
        base_url=BASE,
        fallback_base_urls=["https://fallback.example"],
    )

    assert out["backend"] == "https://fallback.example"
    assert out["rescued_category"] == "it"
    assert out["degraded"] is True
    assert out["degraded_reason"] == "backend_failover"


@respx.mock
async def test_explicit_engines_skip_low_relevance_category_rescue(monkeypatch):
    monkeypatch.setattr(argus.search.semantic, "available", lambda: False)
    calls = 0

    def responder(request):
        nonlocal calls
        calls += 1
        assert request.url.params.get("engines") == "bing"
        return httpx.Response(200, json={"results": [
            {"title": "Fastest cars in the world", "url": "https://e1.com/a",
             "content": "A ranked list of the fastest vehicles.", "engine": "bing"},
            {"title": "Fastest animals on earth", "url": "https://e2.com/b",
             "content": "Speed records from wildlife.", "engine": "bing"},
            {"title": "Fastest internet speed test", "url": "https://e3.com/c",
             "content": "Measure your connection speed.", "engine": "bing"},
        ], "unresponsive_engines": []})

    respx.get(f"{BASE}/search").side_effect = responder

    out = await search(
        "fastest way to parse large JSON in python", base_url=BASE, engines=["bing"]
    )

    assert calls == 2
    assert out["degraded"] is True
    assert out["rescued_category"] is None


@respx.mock
async def test_guard_credits_semantic_rescue(monkeypatch):
    """Hybrid path: zero-lexical-overlap results rescued by high cosine must NOT be flagged
    degraded (else the guard defeats the paraphrase-rescue the hybrid blend exists for)."""
    monkeypatch.setattr(argus.search.semantic, "available", lambda: True)
    monkeypatch.setattr(argus.search.semantic, "similarities", lambda q, texts: [0.9] * len(texts))
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_page([
        {"title": "Alpha", "url": "https://e/1", "content": "one two three", "engine": "bing"},
        {"title": "Beta", "url": "https://e/2", "content": "four five six", "engine": "bing"},
        {"title": "Gamma", "url": "https://e/3", "content": "seven eight nine", "engine": "bing"},
    ])))
    out = await search("quantum entanglement teleportation", base_url=BASE)
    assert out["degraded"] is False
    assert all("_sem_relevant" not in r for r in out["results"])  # transient flag stripped


@respx.mock
async def test_guard_does_not_credit_borderline_semantic_junk(monkeypatch):
    """Keep-floor semantic rescues are useful, but the health guard should only credit
    high-confidence semantic matches. Borderline cosine with zero lexical overlap must
    still mark the result set degraded."""
    monkeypatch.setattr(argus.search.semantic, "available", lambda: True)
    monkeypatch.setattr(argus.search.semantic, "similarities", lambda q, texts: [0.4] * len(texts))
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_page([
        {"title": "Alpha", "url": "https://e/1", "content": "one two three", "engine": "bing"},
        {"title": "Beta", "url": "https://e/2", "content": "four five six", "engine": "bing"},
        {"title": "Gamma", "url": "https://e/3", "content": "seven eight nine", "engine": "bing"},
    ])))
    out = await search("quantum entanglement teleportation", base_url=BASE)
    assert out["degraded"] is True and out["degraded_reason"] == "low_relevance"


@respx.mock
async def test_guard_still_flags_low_cosine_junk(monkeypatch):
    """Hybrid path but genuinely off-topic (low cosine AND no lexical overlap) still degrades."""
    monkeypatch.setattr(argus.search.semantic, "available", lambda: True)
    monkeypatch.setattr(argus.search.semantic, "similarities", lambda q, texts: [0.05] * len(texts))
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=_page([
        {"title": "Alpha", "url": "https://e/1", "content": "one two three", "engine": "bing"},
        {"title": "Beta", "url": "https://e/2", "content": "four five six", "engine": "bing"},
        {"title": "Gamma", "url": "https://e/3", "content": "seven eight nine", "engine": "bing"},
    ])))
    out = await search("quantum entanglement teleportation", base_url=BASE)
    assert out["degraded"] is True and out["degraded_reason"] == "low_relevance"
