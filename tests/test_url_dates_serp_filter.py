"""D5 (date from the URL path, metadata only) and D6 (no SERP pages as research sources)."""

import pytest

import argus.search
from argus.models import STAGE_COUNTS
from argus.research import _is_serp, research
from argus.search import _map_result, _url_date, rerank

ARTICLE_HTML = (
    "<html><head><title>Gold Outlook</title></head><body><article><h1>Gold Outlook</h1>"
    "<p>" + ("Gold prices are driven by real yields and the dollar. " * 8) + "</p>"
    "</article></body></html>"
)


# --------------------------------------------------------------------------- #
# D5: URL path date
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("url", "expected"), [
    ("https://ex.com/2026/10/31/gold-outlook", "2026-10-31"),
    ("https://ex.com/news/2026/10/gold-outlook", "2026-10"),  # month precision kept honest
    ("https://ex.com/2026/10", "2026-10"),
    ("https://ex.com/blog/2026-10-31-gold-outlook", "2026-10-31"),
    ("https://ex.com/2026-10-31/", "2026-10-31"),
    ("https://ex.com/2026-10-31.html", "2026-10-31"),
    ("https://ex.com/archive/20261031/story", "2026-10-31"),
    ("https://ex.com/archive/20261031", "2026-10-31"),
    ("https://ex.com/1995/01/02/x", "1995-01-02"),
    ("https://ex.com/2026/02/30/x/2026/03/x", "2026-03"),  # bad first date, later one ok
])
def test_url_date_positive(url, expected):
    assert _url_date(url) == expected


@pytest.mark.parametrize("url", [
    "https://ex.com/products/2048/",  # product number, no month
    "https://ex.com/products/2048/12/",  # year past current + 1
    "https://ex.com/1994/12/31/x",  # before the web had dated URLs
    "https://ex.com/2026/13/01/x",  # month
    "https://ex.com/2026/02/30/x",  # day: never falls back to month precision
    "https://ex.com/2026/1/5/x",  # single digits: too ambiguous
    "https://ex.com/page?date=2026/10/31",  # query string
    "https://ex.com/x#/2026/10/31",  # fragment
    "https://20261031.ex.com/x",  # hostname
    "https://ex.com/item/202610311",  # nine digits
    "https://ex.com/sku-20261031",  # not a whole segment
    "https://ex.com/v2026-10-311/x",  # not at a segment start
    "https://ex.com/",
    "",
])
def test_url_date_negative(url):
    assert _url_date(url) is None


def test_map_result_infers_url_date_when_engine_sent_none():
    out = _map_result({"url": "https://ex.com/2026/10/31/x", "title": "t", "content": "s"})
    assert out["published"] == "2026-10-31"
    assert out["date_source"] == "url"


def test_map_result_engine_date_wins_and_is_unmarked():
    out = _map_result({"url": "https://ex.com/2026/10/31/x", "title": "t",
                       "publishedDate": "2026-06-01T00:00:00"})
    assert out["published"] == "2026-06-01T00:00:00"
    assert "date_source" not in out


def test_map_result_no_date_anywhere():
    out = _map_result({"url": "https://ex.com/post/x", "title": "t"})
    assert "published" not in out
    assert "date_source" not in out


def _pair(dated_extra: dict | None) -> list[dict]:
    """Two results with equal relevance; only the second may carry a date."""
    a = _map_result({"url": "https://a.example/post/x", "title": "esp32 modbus alpha",
                     "content": "x", "engine": "bing"})
    raw_b = {"url": "https://b.example/post/x", "title": "esp32 modbus beta",
             "content": "x", "engine": "bing"}
    if dated_extra is not None:
        raw_b = {**raw_b, **dated_extra}
    return [a, _map_result(raw_b)]


def _order(results, **kw):
    return [r["title"] for r in rerank("esp32 modbus", results, **kw)]


@pytest.mark.parametrize("recency", [False, True])
def test_url_date_does_not_change_lexical_rank(recency):
    plain = _order(_pair(None), recency=recency, semantic_rerank=False)
    url_dated = _pair({"url": "https://b.example/2026/10/31/x"})
    assert url_dated[1]["date_source"] == "url"
    assert _order(url_dated, recency=recency, semantic_rerank=False) == plain
    # Control: an engine date on the same row does move it, so the check is sensitive.
    engine_dated = _pair({"publishedDate": "2026-10-31T00:00:00"})
    assert _order(engine_dated, recency=recency, semantic_rerank=False) != plain


@pytest.mark.parametrize("recency", [False, True])
def test_url_date_does_not_change_hybrid_rank(monkeypatch, recency):
    monkeypatch.setattr(argus.search.semantic, "similarities",
                        lambda q, docs: [0.7] * len(docs))
    plain = _order(_pair(None), recency=recency, semantic_rerank=True)
    url_dated = _pair({"url": "https://b.example/2026/10/31/x"})
    assert _order(url_dated, recency=recency, semantic_rerank=True) == plain
    engine_dated = _pair({"publishedDate": "2026-10-31T00:00:00"})
    assert _order(engine_dated, recency=recency, semantic_rerank=True) != plain


# --------------------------------------------------------------------------- #
# D6: search-result pages are not research sources
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "https://www.google.com/search?q=gold+outlook",
    "https://www.bing.com/search?q=gold&form=QBLH",
    "https://duckduckgo.com/?q=gold",
    "https://www.youtube.com/results?search_query=gold",
    "https://ex.com/search?query=gold",
    "https://ex.com/en/search/?keyword=gold",
    "https://ex.com/search.php?k=gold",
    "https://ex.com/?s=gold",  # WordPress search
    "https://ex.com?q=gold",  # empty path
    "https://ex.com/Search?Q=gold",  # case-insensitive
    "https://ex.com/results?keywords=gold+price",
])
def test_is_serp_positive(url):
    assert _is_serp(url)


@pytest.mark.parametrize("url", [
    "https://ex.com/2026/10/how-search-works?utm=x",
    "https://ex.com/2026/10/how-search-works?utm_source=search",
    "https://ex.com/?p=123",
    "https://ex.com/search?q=",  # empty value
    "https://ex.com/search?q=+",  # blank value
    "https://ex.com/search",  # no query at all
    "https://ex.com/docs/page?q=gold",  # neither a search segment nor the root
    "https://ex.com/research?q=gold",  # 'research' is not a 'search' segment
    "https://ex.com/search-engines-explained?q=gold",
    "https://html.duckduckgo.com/html?q=gold",  # conservative miss, kept on purpose
    "https://ex.com/watch?v=abc",
    "",
])
def test_is_serp_negative(url):
    assert not _is_serp(url)


def _fake_search(results):
    async def _search(query, count=10):
        return {"query": query, "results": results[:count], "count": len(results[:count])}

    return _search


def _recording_fetch(fetched: list[str]):
    async def _fetch(url, *, client=None, browser=None, timeout=30):
        fetched.append(url)
        return {"final_url": url, "status": 200, "html": ARTICLE_HTML, "render_path": "static"}

    return _fetch


async def test_research_skips_serp_and_still_fills_sources():
    serp = "https://www.google.com/search?q=gold+outlook"
    articles = [f"https://ex{i}.com/gold-{i}" for i in range(1, 4)]
    results = [{"url": u, "title": f"Gold {u}", "snippet": "gold"} for u in [serp, *articles]]
    fetched: list[str] = []
    before = STAGE_COUNTS.get("research.serp_skipped", 0)

    out = await research("gold outlook", max_sources=3, search_fn=_fake_search(results),
                         fetch_fn=_recording_fetch(fetched))

    assert serp not in fetched
    assert [s["url"] for s in out["sources"]] == articles  # SERP slot backfilled
    assert out["failed"] == []
    assert STAGE_COUNTS["research.serp_skipped"] == before + 1


async def test_research_quick_mode_drops_serp_too():
    serp = "https://ex.com/?s=gold"
    results = [{"url": u, "title": u, "snippet": "s"}
               for u in [serp, "https://ex1.com/a", "https://ex2.com/b"]]
    out = await research("gold", mode="quick", max_sources=2,
                         search_fn=_fake_search(results))
    assert [s["url"] for s in out["sources"]] == ["https://ex1.com/a", "https://ex2.com/b"]
