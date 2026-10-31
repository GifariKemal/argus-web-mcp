"""Hacker News (Algolia) and Polymarket (Gamma) adapters + their place in the fetch ladder."""

import httpx
import pytest
import respx

from argus.extract.article import extract_article
from argus.fetch import adapters
from argus.fetch.adapters import fetch_hn, fetch_polymarket, hn_target, polymarket_target
from argus.fetch.core import fetch
from argus.models import STAGE_COUNTS


@pytest.fixture(autouse=True)
def _dns(public_dns):
    """The SSRF guard still resolves every hop; make fixture hosts public."""


# --- Hacker News --------------------------------------------------------------------

HN_URL = "https://news.ycombinator.com/item?id=8863"
HN_API = "https://hn.algolia.com/api/v1/items/8863"


def _c(cid, author, text, children=(), day="2024-01-02"):
    return {"id": cid, "type": "comment", "author": author, "text": text,
            "created_at": f"{day}T10:00:00.000Z", "children": list(children)}


HN_ITEM = {
    "id": 8863, "type": "story", "author": "dhouston", "title": "My YC app: Dropbox",
    "url": "http://www.getdropbox.com/u/2/screencast.html", "points": 111,
    "created_at": "2007-04-04T19:16:40.000Z",
    "text": "Story body with <i>emphasis</i>.<p>Second story paragraph.",
    "children": [
        _c(1, "alice", "Top level comment from alice.", [
            _c(2, "bob", 'Reply from bob, see <a href="https:&#x2F;&#x2F;ok.example&#x2F;doc" '
                         'rel="nofollow">the doc</a>.', [
                _c(3, "carol", "Third level from carol."),
            ]),
        ]),
        # A deleted comment keeps its replies.
        {"id": 4, "type": "comment", "author": None, "text": None,
         "created_at": "2024-01-03T00:00:00.000Z",
         "children": [_c(5, "dave", "Reply under a deleted comment.")]},
    ],
}


@pytest.mark.parametrize(("url", "expected"), [
    (HN_URL, HN_API),
    ("http://news.ycombinator.com/item?id=8863", HN_API),
    ("https://news.ycombinator.com/item?id=8863&p=2", HN_API),
    ("https://news.ycombinator.com/news", None),
    ("https://news.ycombinator.com/item?id=abc", None),
    ("https://news.ycombinator.com/item", None),
    ("https://news.ycombinator.com/user?id=pg", None),
    ("https://news.ycombinator.com.evil.example/item?id=1", None),
    ("https://evil.example/item?id=1", None),
    ("ftp://news.ycombinator.com/item?id=1", None),
])
def test_hn_target(url, expected):
    assert hn_target(url) == expected


@respx.mock
async def test_fetch_hn_builds_story_and_threaded_comments():
    respx.get(HN_API).mock(return_value=httpx.Response(200, json=HN_ITEM))
    async with httpx.AsyncClient() as c:
        res = await fetch_hn(HN_URL, client=c)
    assert res["render_path"] == "api" and res["status"] == 200 and res["final_url"] == HN_URL
    page = res["html"]
    assert "<title>My YC app: Dropbox</title>" in page
    assert 'href="http://www.getdropbox.com/u/2/screencast.html"' in page
    assert '<a href="https:&#x2F;&#x2F;ok.example&#x2F;doc">' in page  # HN's link markup stays
    text = extract_article(page, url=HN_URL)["content"]
    for want in ("My YC app: Dropbox", "111 points", "dhouston", "2007-04-04",
                 "Second story paragraph", "alice", "bob", "carol", "[deleted]",
                 "Reply under a deleted comment."):
        assert want in text
    assert "Story body with *emphasis*." in text and text.count("Second story paragraph") == 1
    # Nesting survives extraction: each reply is indented under its parent.
    lines = text.splitlines()
    indent = {n: next(len(x) - len(x.lstrip()) for x in lines if f"**{n}**" in x)
              for n in ("alice", "bob", "carol")}
    assert indent["alice"] < indent["bob"] < indent["carol"]
    assert "not shown" not in text
    assert res["structured"] == {"id": 8863, "comments_total": 5, "comments_shown": 5}


@respx.mock
async def test_fetch_hn_caps_depth_and_count_and_says_so():
    chain = _c(1000, "deep", "level 1")
    node = chain
    for d in range(2, 11):  # 10 levels: 2 past the depth cap
        child = _c(1000 + d, "deep", f"level {d}")
        node["children"] = [child]
        node = child
    flat = [_c(i, f"u{i}", f"flat {i}") for i in range(600)]
    item = {**HN_ITEM, "children": [chain, *flat]}
    respx.get(HN_API).mock(return_value=httpx.Response(200, json=item))
    async with httpx.AsyncClient() as c:
        res = await fetch_hn(HN_URL, client=c)
    page = res["html"]
    assert "level 8" in page and "level 9" not in page
    # 8 of the chain + 492 flat = 500 shown; 2 deep + 108 flat hidden.
    assert "flat 491" in page and "flat 492" not in page
    assert "110 more comments not shown" in page
    assert res["structured"] == {"id": 8863, "comments_total": 610, "comments_shown": 500}


@respx.mock
async def test_fetch_hn_escapes_unsafe_markup():
    evil = ('<script>alert(1)</script><img src=x onerror=alert(2)>'
            '<a href="javascript:alert(3)">x</a> a < b')
    item = {**HN_ITEM, "title": "<script>t()</script>", "author": "<b>x</b>",
            "url": "javascript:alert(4)", "children": [_c(1, "<i>m</i>", evil)]}
    respx.get(HN_API).mock(return_value=httpx.Response(200, json=item))
    async with httpx.AsyncClient() as c:
        res = await fetch_hn(HN_URL, client=c)
    page = res["html"].lower()
    assert "<script" not in page and "<img" not in page and 'href="javascript:' not in page
    assert "<b>x</b>" not in page and "<i>m</i>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "a &lt; b" in page


@pytest.mark.parametrize("resp", [
    httpx.Response(500),
    httpx.Response(404, json={"status": 404, "error": "Not Found"}),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=[]),
    httpx.ConnectError("down"),
])
@respx.mock
async def test_fetch_hn_api_failure_returns_none(resp):
    respx.get(HN_API).mock(side_effect=[resp])
    async with httpx.AsyncClient() as c:
        assert await fetch_hn(HN_URL, client=c) is None


async def test_fetch_hn_non_matching_url_makes_no_request():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: 1 / 0)) as c:
        assert await fetch_hn("https://news.ycombinator.com/news", client=c) is None


@respx.mock
async def test_ladder_reads_hn_from_api_before_the_page():
    page = respx.get(HN_URL).mock(return_value=httpx.Response(200, text="<html>x</html>"))
    respx.get(HN_API).mock(return_value=httpx.Response(200, json=HN_ITEM))
    before = STAGE_COUNTS.get("fetch.adapter_ok", 0)
    async with httpx.AsyncClient() as c:
        res = await fetch(HN_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "api" and page.call_count == 0
    assert STAGE_COUNTS["fetch.adapter_ok"] == before + 1


@respx.mock
async def test_ladder_falls_back_to_hn_page_when_api_fails():
    respx.get(HN_API).mock(return_value=httpx.Response(502))
    respx.get(HN_URL).mock(return_value=httpx.Response(
        200, text="<html><body><article>" + "thread text " * 400 + "</article></body></html>"))
    before = STAGE_COUNTS.get("fetch.adapter_fail", 0)
    async with httpx.AsyncClient() as c:
        res = await fetch(HN_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "static"
    assert STAGE_COUNTS["fetch.adapter_fail"] == before + 1


# --- Polymarket ---------------------------------------------------------------------

PM_SLUG = "fed-decision-in-december"
PM_URL = f"https://polymarket.com/event/{PM_SLUG}"
PM_API = "https://gamma-api.polymarket.com/events"


def _m(question, outcomes, prices, *, closed=False, volume="12345.6"):
    return {"question": question, "outcomes": outcomes, "outcomePrices": prices,
            "volume": volume, "active": True, "closed": closed}


PM_EVENT = [{
    "title": "Fed decision in December?", "endDate": "2026-12-10T12:00:00Z",
    "volume": 98765432.1, "liquidity": "1234567.8",
    "description": "This market resolves on the <b>FOMC</b> statement.\nSecond line.",
    "markets": [
        _m("Fed cuts 25 bps?", '["Yes", "No"]', '["0.029", "0.971"]'),
        _m("Fed holds?", '["Yes", "No"]', '["1", "0"]', closed=True, volume=500),
        _m("Broken market?", "[not json", '["0.5", "0.5"]'),
        _m("Broken prices?", '["Yes", "No"]', "nope"),
    ],
}]


@pytest.mark.parametrize(("url", "expected"), [
    (PM_URL, PM_SLUG),
    (f"https://www.polymarket.com/event/{PM_SLUG}/", PM_SLUG),
    (f"{PM_URL}/fed-cuts-25", PM_SLUG),
    (f"{PM_URL}?tid=17", PM_SLUG),
    ("https://polymarket.com/event/", None),
    ("https://polymarket.com/event/Fed_Decision", None),
    ("https://polymarket.com/event/a%2Fb", None),
    ("https://polymarket.com/event/a/b/c", None),
    ("https://polymarket.com/markets", None),
    ("https://polymarket.com.evil.example/event/x", None),
    ("ftp://polymarket.com/event/x", None),
])
def test_polymarket_target(url, expected):
    assert polymarket_target(url) == expected


@respx.mock
async def test_fetch_polymarket_builds_event_page():
    route = respx.get(PM_API).mock(return_value=httpx.Response(200, json=PM_EVENT))
    async with httpx.AsyncClient() as c:
        res = await fetch_polymarket(PM_URL, client=c)
    assert route.calls.last.request.url.params["slug"] == PM_SLUG
    assert res["render_path"] == "api" and res["status"] == 200 and res["final_url"] == PM_URL
    assert "<b>FOMC</b>" not in res["html"]  # description is text, not markup
    text = extract_article(res["html"], url=PM_URL)["content"]
    for want in ("Fed decision in December?", "2026-12-10", "$98,765,432", "$1,234,568",
                 "Second line.", "Fed cuts 25 bps?", "Yes 2.9%", "No 97.1%", "$12,346",
                 "active", "Fed holds?", "Yes 100.0%", "closed", "$500"):
        assert want in text, want
    assert "Broken market?" not in text and "Broken prices?" not in text
    assert res["structured"] == {
        "slug": PM_SLUG, "title": "Fed decision in December?",
        "end_date": "2026-12-10T12:00:00Z", "volume": 98765432.1, "liquidity": 1234567.8,
        "markets": [
            {"question": "Fed cuts 25 bps?", "active": True, "closed": False,
             "volume": 12345.6, "outcomes": [{"name": "Yes", "price": 0.029},
                                             {"name": "No", "price": 0.971}]},
            {"question": "Fed holds?", "active": True, "closed": True, "volume": 500.0,
             "outcomes": [{"name": "Yes", "price": 1.0}, {"name": "No", "price": 0.0}]},
        ]}


@respx.mock
async def test_fetch_polymarket_missing_numbers_are_none():
    event = [{"title": "T", "volume": "n/a", "markets": [
        {"question": "Q", "outcomes": ["Yes"], "outcomePrices": ["0.5"], "volume": None}]}]
    respx.get(PM_API).mock(return_value=httpx.Response(200, json=event))
    async with httpx.AsyncClient() as c:
        res = await fetch_polymarket(PM_URL, client=c)
    s = res["structured"]
    assert s["volume"] is None and s["liquidity"] is None and s["end_date"] is None
    assert s["markets"] == [{"question": "Q", "active": None, "closed": None, "volume": None,
                             "outcomes": [{"name": "Yes", "price": 0.5}]}]
    assert "Volume n/a" in res["html"]


@pytest.mark.parametrize("resp", [
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"error": "x"}),
    httpx.Response(200, text="not json"),
    httpx.Response(500),
    httpx.ConnectError("down"),
])
@respx.mock
async def test_fetch_polymarket_failure_returns_none(resp):
    respx.get(PM_API).mock(side_effect=[resp])
    async with httpx.AsyncClient() as c:
        assert await fetch_polymarket(PM_URL, client=c) is None


@respx.mock
async def test_ladder_reads_polymarket_from_api_before_the_page():
    page = respx.get(PM_URL).mock(return_value=httpx.Response(200, text="<html>x</html>"))
    respx.get(PM_API).mock(return_value=httpx.Response(200, json=PM_EVENT))
    async with httpx.AsyncClient() as c:
        res = await fetch(PM_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "api" and page.call_count == 0


@respx.mock
async def test_ladder_falls_back_to_polymarket_page_when_api_empty():
    respx.get(PM_API).mock(return_value=httpx.Response(200, json=[]))
    respx.get(PM_URL).mock(return_value=httpx.Response(
        200, text="<html><body><article>" + "market text " * 400 + "</article></body></html>"))
    async with httpx.AsyncClient() as c:
        res = await fetch(PM_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "static"


def test_caps_are_the_documented_ones():
    assert adapters._HN_MAX_DEPTH == 8 and adapters._HN_MAX_COMMENTS == 500
