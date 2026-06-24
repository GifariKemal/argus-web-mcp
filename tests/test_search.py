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
async def test_optional_params_absent_when_unset():
    captured = {}

    def responder(request):
        captured.update(_query_of(request))
        return httpx.Response(200, json=_page([_result(1)]))

    respx.get(f"{BASE}/search").side_effect = responder

    await search("q", base_url=BASE)

    assert "time_range" not in captured
    assert "language" not in captured
    assert captured["categories"] == ["general"]


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
    # same URL modulo scheme + trailing slash + query string
    dup = _mapped("ESP-Claw A mirror", "http://example.com/esp-claw/?utm=1", "esp claw")
    out = rerank("esp-claw", [a, dup])
    assert len(out) == 1
    assert out[0]["url"] == "https://example.com/esp-claw"  # first kept


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
