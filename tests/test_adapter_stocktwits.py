"""StockTwits symbol stream adapter (keyless JSON API) and its place in the fetch ladder."""

import httpx
import pytest
import respx

from argus.extract.article import extract_article
from argus.fetch.adapters import fetch_stocktwits, stocktwits_target
from argus.fetch.core import fetch
from argus.models import STAGE_COUNTS


@pytest.fixture(autouse=True)
def _dns(public_dns):
    """The SSRF guard still resolves every hop; make fixture hosts public."""


ST_URL = "https://stocktwits.com/symbol/AAPL"
ST_API = "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json"


def _msg(mid, body, created, user="trader", sentiment=None, likes=0, official=False):
    return {"id": mid, "body": body, "created_at": created,
            "user": {"username": user, "followers": 10, "official": official},
            "entities": {"sentiment": {"basic": sentiment} if sentiment else None},
            "likes": {"total": likes}}


ST_DOC = {
    "response": {"status": 200},
    "symbol": {"symbol": "AAPL", "title": "Apple Inc.", "exchange": "NASDAQ",
               "watchlist_count": 1234567, "sentiment_change": 12.5, "volume_change": -3.2},
    "cursor": {"more": True, "since": 3, "max": 1},
    "messages": [  # older first here: the page must still list newest first
        _msg(1, "$AAPL older &amp; wiser", "2026-10-31T09:00:00Z", sentiment="Bearish"),
        _msg(3, "$AAPL it&#39;s <script>alert(1)</script> &lt;b&gt;bold&lt;/b&gt;",
             "2026-10-31T11:00:00Z", user="StockTwits", official=True, likes=7,
             sentiment="Bullish"),
        _msg(2, "$AAPL no label here", "2026-10-31T10:00:00Z"),
    ],
}


@pytest.mark.parametrize(("url", "expected"), [
    (ST_URL, "AAPL"),
    ("https://stocktwits.com/symbol/aapl/", "AAPL"),
    ("https://www.stocktwits.com/symbol/BRK.B?utm=x", "BRK.B"),
    ("http://stocktwits.com/symbol/BTC.X", "BTC.X"),
    ("https://stocktwits.com/symbol/SPY-W", "SPY-W"),
    ("https://stocktwits.com/symbol/", None),
    ("https://stocktwits.com/symbol/..", None),
    ("https://stocktwits.com/symbol/A_B", None),
    ("https://stocktwits.com/symbol/ABCDEFGHIJKLMNOPQRSTU", None),
    ("https://stocktwits.com/symbol/AAPL/news", None),
    ("https://stocktwits.com/trader", None),
    ("https://stocktwits.com.evil.example/symbol/AAPL", None),
    ("ftp://stocktwits.com/symbol/AAPL", None),
])
def test_stocktwits_target(url, expected):
    assert stocktwits_target(url) == expected


@respx.mock
async def test_fetch_stocktwits_builds_stream_page():
    respx.get(ST_API).mock(return_value=httpx.Response(200, json=ST_DOC))
    async with httpx.AsyncClient() as c:
        res = await fetch_stocktwits("https://stocktwits.com/symbol/aapl", client=c)
    assert res["render_path"] == "api" and res["status"] == 200
    page = res["html"]
    assert "<script" not in page and "<b>bold" not in page
    assert "&lt;script&gt;" in page and "&amp;amp;" not in page
    text = extract_article(page, url=ST_URL)["content"]
    for want in ("AAPL", "Apple Inc.", "NASDAQ", "1,234,567", "bullish 1", "bearish 1",
                 "unlabeled 1", "12.5", "-3.2", "@StockTwits (official)", "7 likes",
                 "it's", "older & wiser"):
        assert want in text, want
    assert text.index("it's") < text.index("no label here") < text.index("older & wiser")
    assert "not shown" not in text
    s = res["structured"]
    assert {k: v for k, v in s.items() if k != "messages"} == {
        "symbol": "AAPL", "title": "Apple Inc.", "exchange": "NASDAQ",
        "watchlist_count": 1234567, "bullish": 1, "bearish": 1, "unlabeled": 1,
        "sentiment_change": 12.5, "volume_change": -3.2}
    assert [m["id"] for m in s["messages"]] == [3, 2, 1]
    assert s["messages"][0] == {
        "id": 3, "created_at": "2026-10-31T11:00:00Z", "user": "StockTwits",
        "official": True, "sentiment": "Bullish", "likes": 7,
        "body": "$AAPL it's <script>alert(1)</script> <b>bold</b>"}
    assert s["messages"][1]["sentiment"] is None


@respx.mock
async def test_fetch_stocktwits_caps_messages_and_says_so():
    msgs = [_msg(i, f"msg {i}", f"2026-10-31T10:{i:02d}:00Z") for i in range(35)]
    respx.get(ST_API).mock(return_value=httpx.Response(200, json={**ST_DOC, "messages": msgs}))
    async with httpx.AsyncClient() as c:
        res = await fetch_stocktwits(ST_URL, client=c)
    assert len(res["structured"]["messages"]) == 30 and res["structured"]["unlabeled"] == 30
    assert "msg 34" in res["html"] and "msg 4<" not in res["html"]
    assert "5 more messages not shown" in res["html"]


@pytest.mark.parametrize("resp", [
    httpx.Response(429, text="rate limited"),
    httpx.Response(404, json={"response": {"status": 404}}),
    httpx.Response(500),
    httpx.Response(200, json={**ST_DOC, "response": {"status": 404}}),
    httpx.Response(200, json={k: v for k, v in ST_DOC.items() if k != "response"}),
    httpx.Response(200, json={k: v for k, v in ST_DOC.items() if k != "symbol"}),
    httpx.Response(200, json={**ST_DOC, "messages": "nope"}),
    httpx.Response(200, json=[]),
    httpx.Response(200, text="not json"),
    httpx.ConnectError("down"),
])
@respx.mock
async def test_fetch_stocktwits_failure_returns_none(resp):
    route = respx.get(ST_API).mock(side_effect=[resp, httpx.Response(200, json=ST_DOC)])
    async with httpx.AsyncClient() as c:
        assert await fetch_stocktwits(ST_URL, client=c) is None
    assert route.call_count == 1  # never retried, the 429 included


async def test_fetch_stocktwits_non_matching_url_makes_no_request():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: 1 / 0)) as c:
        assert await fetch_stocktwits("https://stocktwits.com/trader", client=c) is None


@respx.mock
async def test_ladder_reads_stocktwits_from_api_before_the_page():
    page = respx.get(ST_URL).mock(return_value=httpx.Response(200, text="<html>x</html>"))
    respx.get(ST_API).mock(return_value=httpx.Response(200, json=ST_DOC))
    before = STAGE_COUNTS.get("fetch.adapter_ok", 0)
    async with httpx.AsyncClient() as c:
        res = await fetch(ST_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "api" and page.call_count == 0
    assert STAGE_COUNTS["fetch.adapter_ok"] == before + 1


@respx.mock
async def test_ladder_falls_back_to_page_when_rate_limited():
    respx.get(ST_API).mock(return_value=httpx.Response(429))
    respx.get(ST_URL).mock(return_value=httpx.Response(
        200, text="<html><body><article>" + "symbol text " * 400 + "</article></body></html>"))
    before = STAGE_COUNTS.get("fetch.adapter_fail", 0)
    async with httpx.AsyncClient() as c:
        res = await fetch(ST_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "static"
    assert STAGE_COUNTS["fetch.adapter_fail"] == before + 1


@respx.mock
async def test_stocktwits_api_is_asked_with_a_browser_user_agent():
    """The stream API answered 403 to Argus's own UA from the server (2026-10-31) and 200
    to a browser UA; only this one API call carries it."""
    route = respx.get(ST_API).mock(return_value=httpx.Response(200, json=ST_DOC))
    async with httpx.AsyncClient() as c:
        await fetch_stocktwits("https://stocktwits.com/symbol/aapl", client=c)
    ua = route.calls[0].request.headers["user-agent"]
    assert ua.startswith("Mozilla/5.0") and "Chrome/" in ua
    assert route.calls[0].request.headers["accept"] == "application/json"
