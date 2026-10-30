"""X module, Layer 0: FxTwitter client, URL targeting, post model (offline via respx)."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from argus import config, server
from argus.fetch.core import fetch
from argus.x.fxtwitter import fx_post, fx_user, post_from_fx, render_html, x_target
from conftest import ARTICLE_HTML, FakeBrowser

FIX = Path(__file__).parent / "fixtures" / "x"
FX = config.X_FXTWITTER_URL


def load(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


@pytest.fixture(autouse=True)
def _dns(public_dns):
    """The SSRF guard still resolves every hop; make fixture hosts public."""


@pytest.mark.parametrize(("url", "expected"), [
    ("https://x.com/jack/status/20", ("status", "20")),
    ("https://twitter.com/jack/status/20?s=20", ("status", "20")),
    ("https://mobile.twitter.com/jack/statuses/20", ("status", "20")),
    ("https://www.x.com/cnxsoft/status/1957667536302141541/photo/1",
     ("status", "1957667536302141541")),
    ("https://x.com/i/web/status/20", ("status", "20")),
    ("https://x.com/XDevelopers", ("user", "XDevelopers")),
    ("https://x.com/XDevelopers/", ("user", "XDevelopers")),
    ("https://x.com/home", None),
    ("https://x.com/search?q=a", None),
    ("https://x.com/explore", None),
    ("https://x.com/i/flow/signup", None),
    ("https://x.com/settings", None),
    ("https://x.com/hashtag/esp32", None),
    ("https://x.com/jack/likes", None),
    ("https://x.com.evil.example/jack/status/20", None),
    ("https://notx.com/jack/status/20", None),
    ("http://example.com/jack/status/20", None),
    ("https://x.com/a_name_way_too_long_x/status/1", None),
    ("ftp://x.com/jack/status/20", None),
])
def test_x_target(url, expected):
    assert x_target(url) == expected


def test_post_from_fx_media_fixture():
    p = post_from_fx(load("fx_status_media.json")["tweet"])
    assert p["id"] == "1957667536302141541"
    assert p["url"] == "https://x.com/cnxsoft/status/1957667536302141541"
    assert p["created_at"] == "2025-08-19T04:54:47Z"
    assert p["text"].startswith("ESP32 Modbus industrial IoT gateway")
    assert "https://www.cnx-software.com/2025/08/19/" in p["text"]  # expanded, not t.co
    assert "t.co/" not in p["text"]
    assert p["lang"] == "en"
    assert p["author"]["handle"] == "cnxsoft" and p["author"]["verified"] is True
    assert p["metrics"] == {"replies": 1, "reposts": 33, "likes": 295, "quotes": 0,
                            "views": 12319, "bookmarks": 117}
    assert p["media"][0] == {
        "type": "photo", "url": "https://pbs.twimg.com/media/GysIKg0bMAAgnP7.jpg?name=orig"}
    assert p["quoted"] is None and p["in_reply_to"] is None and p["source"] == "fxtwitter"


def test_post_from_fx_maps_a_quoted_post():
    inner = load("fx_status_plain.json")["tweet"]
    outer = {**load("fx_status_media.json")["tweet"], "quote": inner}
    assert post_from_fx(outer)["quoted"]["id"] == "20"


@respx.mock
async def test_fx_post_and_user():
    respx.get(f"{FX}/status/20").mock(
        return_value=httpx.Response(200, json=load("fx_status_plain.json")))
    respx.get(f"{FX}/XDevelopers").mock(return_value=httpx.Response(200, json=load("fx_user.json")))
    async with httpx.AsyncClient() as c:
        post = await fx_post("20", client=c)
        user = await fx_user("XDevelopers", client=c)
    assert post["text"] == "just setting up my twttr" and post["author"]["handle"] == "jack"
    assert user["handle"] == "XDevelopers" and user["followers"] == 697736
    assert user["posts"] == 4381 and user["verified"] is True
    assert user["url"] == "https://x.com/XDevelopers"


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(200, text=load("fx_not_found.html"), headers={"content-type": "text/html"}),
    httpx.Response(500, text="boom"),
    httpx.Response(200, json={"code": 404, "message": "NOT_FOUND"}),
    httpx.Response(200, json={"code": 200, "message": "OK"}),
])
async def test_fx_post_non_json_is_none(response):
    respx.get(f"{FX}/status/20").mock(return_value=response)
    respx.get(f"{FX}/jack").mock(return_value=response)
    async with httpx.AsyncClient() as c:
        assert await fx_post("20", client=c) is None
        assert await fx_user("jack", client=c) is None


# --- Task 2: read routes x.com URLs through FxTwitter -------------------------------

def _client(fx_status: httpx.Response, seen: list[str]) -> httpx.AsyncClient:
    """FxTwitter answers ``fx_status``; x.com answers a rich article page."""

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "api.fxtwitter.com":
            if req.url.path == "/XDevelopers":
                return httpx.Response(200, json=load("fx_user.json"))
            return fx_status
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_html_escapes_post_text():
    p = post_from_fx(load("fx_status_plain.json")["tweet"]) | {
        "text": "<script>x()</script></article>"}
    h = render_html(post=p)
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert h.count("</article>") == 1


async def test_read_x_status_uses_fxtwitter(app_state):
    seen: list[str] = []
    app_state.client = _client(httpx.Response(200, json=load("fx_status_plain.json")), seen)
    r = await server.read("https://x.com/jack/status/20")
    assert r["render_path"] == "api" and "just setting up my twttr" in r["content"]
    assert r["metadata"]["structured"]["id"] == "20"
    assert seen == [f"{FX}/status/20"]  # the noisy x.com page is never fetched


async def test_read_x_profile_uses_fxtwitter(app_state):
    seen: list[str] = []
    app_state.client = _client(httpx.Response(500), seen)
    r = await server.read("https://x.com/XDevelopers")
    assert r["render_path"] == "api" and "The voice of the X Dev team" in r["content"]
    assert r["metadata"]["structured"]["handle"] == "XDevelopers"


async def test_falls_back_to_static_when_fxtwitter_fails(public_dns):
    seen: list[str] = []
    async with _client(httpx.Response(200, text=load("fx_not_found.html")), seen) as c:
        res = await fetch("https://x.com/jack/status/20", client=c)
    assert res["render_path"] == "static"
    assert seen == [f"{FX}/status/20", "https://x.com/jack/status/20"]


async def test_forced_render_skips_x_adapter(public_dns):
    seen: list[str] = []
    browser = FakeBrowser()
    async with _client(httpx.Response(200, json=load("fx_status_plain.json")), seen) as c:
        res = await fetch("https://x.com/jack/status/20", client=c, browser=browser, render=True)
    assert res["render_path"] == "browser" and browser.calls == 1 and seen == []


# --- final-review fixes ----------------------------------------------------------

MALFORMED = [
    {"text": None},
    {"author": {"name": None}},
    {"media": [None]},
    {"media": {"all": [None]}},
    {"created_timestamp": 10 ** 20},
]


@pytest.mark.parametrize("patch", MALFORMED)
async def test_malformed_fx_post_falls_back_to_static(public_dns, patch):
    tweet = {**load("fx_status_plain.json")["tweet"], **patch}
    seen: list[str] = []
    async with _client(httpx.Response(200, json={"code": 200, "tweet": tweet}), seen) as c:
        res = await fetch("https://x.com/jack/status/20", client=c)
    assert res["render_path"] in ("api", "static")  # never an exception
    if res["render_path"] == "static":
        assert seen[-1] == "https://x.com/jack/status/20"


async def test_fetch_x_gets_a_short_timeout(public_dns, monkeypatch):
    from argus.fetch import core
    from argus.x.fxtwitter import FX_TIMEOUT_S

    got = {}

    async def rec(url, *, client, timeout=30):
        got["timeout"] = timeout
        return None

    monkeypatch.setattr(core, "fetch_x", rec)
    seen: list[str] = []
    async with _client(httpx.Response(500), seen) as c:
        await fetch("https://x.com/jack/status/20", client=c, timeout=60)
    assert got["timeout"] == FX_TIMEOUT_S <= 10
