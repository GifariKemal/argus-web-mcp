"""StackExchange API adapter + its place in the fetch ladder (offline via respx)."""

import httpx
import pytest
import respx

from argus.extract.article import extract_article
from argus.fetch.adapters import fetch_stackexchange, stackexchange_target
from argus.fetch.core import fetch
from argus.models import STAGE_COUNTS

Q_URL = "https://stackoverflow.com/questions/74173750/some-slug"
API_Q = "https://api.stackexchange.com/2.3/questions/74173750"
API_A = "https://api.stackexchange.com/2.3/questions/74173750/answers"

QUESTION = {"items": [{"title": "How do I &quot;x&quot;?", "body": "<p>Question text body.</p>"}]}
ANSWERS = {"items": [
    {"score": 42, "is_accepted": True, "body": "<p>Accepted answer text.</p>"},
    {"score": 7, "is_accepted": False, "body": "<p>Second answer text.</p>"},
]}


@pytest.fixture(autouse=True)
def _dns(public_dns):
    """The SSRF guard still resolves every hop; make fixture hosts public."""


@pytest.mark.parametrize(("url", "expected"), [
    (Q_URL, ("stackoverflow", "74173750")),
    ("https://www.stackoverflow.com/q/1", ("stackoverflow", "1")),
    ("https://superuser.com/questions/22/", ("superuser", "22")),
    ("https://serverfault.com/questions/3", ("serverfault", "3")),
    ("https://askubuntu.com/questions/4/x", ("askubuntu", "4")),
    ("https://math.stackexchange.com/questions/5/y", ("math", "5")),
    ("https://stackoverflow.com/questions/tagged/python", None),
    ("https://stackoverflow.com/users/6/me", None),
    ("https://stackoverflow.com/questions/12abc", None),
    ("https://stackoverflow.com.evil.example/questions/7", None),
    ("https://a.b.stackexchange.com/questions/8", None),
    ("https://example.com/questions/9", None),
])
def test_stackexchange_target(url, expected):
    assert stackexchange_target(url) == expected


@respx.mock
async def test_builds_html_from_question_and_answers():
    q = respx.get(API_Q).mock(return_value=httpx.Response(200, json=QUESTION))
    a = respx.get(API_A).mock(return_value=httpx.Response(200, json=ANSWERS))
    async with httpx.AsyncClient() as c:
        res = await fetch_stackexchange(Q_URL, client=c)
    assert q.calls.last.request.url.params["site"] == "stackoverflow"
    assert q.calls.last.request.url.params["filter"] == "withbody"
    assert a.calls.last.request.url.params["sort"] == "votes"
    assert a.calls.last.request.url.params["pagesize"] == "5"
    assert res["render_path"] == "api" and res["status"] == 200 and res["final_url"] == Q_URL
    html = res["html"]
    assert "<title>How do I &quot;x&quot;?</title>" in html
    assert "Answer (score 42, accepted)" in html and "Answer (score 7)</h3>" in html
    assert html.index("Question text") < html.index("Accepted answer") < html.index("Second")
    assert "Accepted answer text." in extract_article(html, url=Q_URL)["content"]


@pytest.mark.parametrize("q_resp", [
    httpx.Response(400, json={"error_id": 400, "error_message": "bad"}),
    httpx.Response(200, json={"items": []}),
    httpx.Response(200, text="not json"),
    httpx.ConnectError("down"),
])
@respx.mock
async def test_api_failure_returns_none(q_resp):
    respx.get(API_Q).mock(side_effect=[q_resp])
    respx.get(API_A).mock(return_value=httpx.Response(200, json=ANSWERS))
    async with httpx.AsyncClient() as c:
        assert await fetch_stackexchange(Q_URL, client=c) is None


async def test_non_matching_url_makes_no_request():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: 1 / 0)) as c:
        assert await fetch_stackexchange("https://example.com/questions/1", client=c) is None


@respx.mock
async def test_ladder_uses_adapter_after_static_403():
    respx.get(Q_URL).mock(return_value=httpx.Response(403, text="blocked"))
    respx.get(API_Q).mock(return_value=httpx.Response(200, json=QUESTION))
    respx.get(API_A).mock(return_value=httpx.Response(200, json=ANSWERS))
    before = STAGE_COUNTS.get("fetch.adapter_ok", 0)
    async with httpx.AsyncClient() as c:
        res = await fetch(Q_URL, client=c)
    assert res["render_path"] == "api"
    assert STAGE_COUNTS["fetch.adapter_ok"] == before + 1


@respx.mock
async def test_ladder_falls_through_when_adapter_fails():
    respx.get(Q_URL).mock(return_value=httpx.Response(403, text="blocked"))
    respx.get(API_Q).mock(return_value=httpx.Response(502))
    respx.get(url__startswith="https://archive.org/").mock(
        return_value=httpx.Response(200, json={"archived_snapshots": {}}))
    before = STAGE_COUNTS.get("fetch.adapter_fail", 0)
    async with httpx.AsyncClient() as c:
        with pytest.raises(Exception, match="403"):
            await fetch(Q_URL, client=c)
    assert STAGE_COUNTS["fetch.adapter_fail"] == before + 1


async def test_backoff_from_the_api_pauses_the_adapter(monkeypatch):
    from argus.fetch import adapters

    calls = []

    async def fake_static(url, client=None, timeout=None):
        calls.append(url)
        return {"html": '{"items": [], "backoff": 30}'}

    monkeypatch.setattr(adapters, "fetch_static", fake_static)
    monkeypatch.setattr(adapters, "_backoff_until", 0.0)
    url = "https://stackoverflow.com/questions/1/x"
    assert await adapters.fetch_stackexchange(url, client=None) is None  # empty items
    assert await adapters.fetch_stackexchange(url, client=None) is None
    assert len(calls) == 1  # second call skipped: the API asked for 30 s of quiet


# --- npm registry adapter -----------------------------------------------------------
from argus.fetch.adapters import fetch_npm, npm_target  # noqa: E402

NPM_URL = "https://www.npmjs.com/package/@scope/pkg-name"
NPM_API = "https://registry.npmjs.org/@scope/pkg-name"
NPM_DOC = {"name": "@scope/pkg-name", "description": "A tiny package.",
           "homepage": "https://example.com/pkg", "dist-tags": {"latest": "2.1.0"},
           "readme": "# pkg-name\n\nInstall with `npm i @scope/pkg-name`.\n"}


@pytest.mark.parametrize(("url", "expected"), [
    (NPM_URL, "@scope/pkg-name"),
    ("https://npmjs.com/package/react/v/18.3.1", "react"),
    ("https://www.npmjs.com/search?q=react", None),
    ("https://www.npmjs.com/~user", None),
    ("https://npmjs.com.evil.example/package/react", None),
])
def test_npm_target(url, expected):
    assert npm_target(url) == expected


@respx.mock
async def test_fetch_npm_builds_readme_page():
    respx.get(NPM_API).mock(return_value=httpx.Response(200, json=NPM_DOC))
    async with httpx.AsyncClient() as c:
        res = await fetch_npm(NPM_URL, client=c)
    assert res["render_path"] == "api" and res["final_url"] == NPM_URL
    art = extract_article(res["html"], NPM_URL)
    assert "npm i @scope/pkg-name" in art["content"] and "2.1.0" in art["content"]


@respx.mock
async def test_fetch_npm_degrades_to_none():
    respx.get(NPM_API).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as c:
        assert await fetch_npm(NPM_URL, client=c) is None


@respx.mock
async def test_fetch_ladder_uses_npm_adapter_when_page_blocks():
    respx.get(NPM_URL).mock(return_value=httpx.Response(403, text="blocked"))
    respx.get(NPM_API).mock(return_value=httpx.Response(200, json=NPM_DOC))
    async with httpx.AsyncClient() as c:
        res = await fetch(NPM_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "api" and "A tiny package." in res["html"]


@respx.mock
async def test_fetch_npm_escapes_registry_markup():
    doc = {**NPM_DOC, "readme": "x </pre><script>alert(1)</script> if a < b"}
    respx.get(NPM_API).mock(return_value=httpx.Response(200, json=doc))
    async with httpx.AsyncClient() as c:
        res = await fetch_npm(NPM_URL, client=c)
    assert "<script>" not in res["html"] and "&lt;/pre&gt;" in res["html"]


# --- reddit thread feed ------------------------------------------------------------

from argus.fetch import adapters as _adapters  # noqa: E402
from argus.fetch.adapters import fetch_reddit, reddit_target  # noqa: E402

RD_URL = "https://www.reddit.com/r/esp32/comments/141sy2s/modbus_on_arduino_ide/"
RD_FEED = "https://www.reddit.com/r/esp32/comments/141sy2s/.rss"
_SC = ("&lt;!-- SC_OFF --&gt;&lt;div class=&quot;md&quot;&gt;&lt;p&gt;{}"
       "&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt;")
_EVIL = "Use esp-modbus &amp;lt;script&amp;gt; from Espressif."
RD_ATOM = f"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Modbus on Arduino IDE : esp32</title>
<entry><author><name>/u/kojotera</name></author><title>Modbus on Arduino IDE</title>
<updated>2023-06-05T19:03:00+00:00</updated>
<content type="html">{_SC.format("Which Modbus RS485 library is reliable?")} submitted by
&lt;a href="x"&gt;/u/kojotera&lt;/a&gt; [link] [comments]</content></entry>
<entry><author><name>/u/helper</name></author><title>/u/helper on Modbus</title>
<updated>2023-06-06T08:00:00+00:00</updated>
<content type="html">{_SC.format(_EVIL)}</content></entry>
</feed>"""


@pytest.fixture
def _reddit_fresh(monkeypatch):
    monkeypatch.setattr(_adapters, "_reddit_backoff_until", 0.0)


@pytest.mark.parametrize(("url", "expected"), [
    (RD_URL, RD_FEED),
    ("https://old.reddit.com/r/esp32/comments/141sy2s", RD_FEED),
    ("https://reddit.com/r/esp32/comments/141sy2s/x/jn1abcd/", RD_FEED),
    ("https://www.reddit.com/r/esp32/", None),
    ("https://www.reddit.com/user/someone/comments/abc/", None),
    ("https://reddit.com.evil.example/r/esp32/comments/141sy2s/", None),
    ("ftp://www.reddit.com/r/esp32/comments/141sy2s/", None),
])
def test_reddit_target(url, expected):
    assert reddit_target(url) == expected


@respx.mock
async def test_fetch_reddit_builds_post_and_comments(_reddit_fresh):
    respx.get(RD_FEED).mock(return_value=httpx.Response(
        200, text=RD_ATOM, headers={"content-type": "application/atom+xml"}))
    async with httpx.AsyncClient() as c:
        res = await fetch_reddit(RD_URL, client=c)
    page = res["html"]
    assert res["render_path"] == "api" and res["final_url"] == RD_URL
    assert "Which Modbus RS485 library is reliable?" in page and "/u/helper" in page
    assert "submitted by" not in page and "<script>" not in page
    text = extract_article(page, url=RD_URL)["content"]
    assert "Use esp-modbus" in text and "Comments" in text


@respx.mock
async def test_fetch_reddit_429_pauses_the_adapter(_reddit_fresh):
    route = respx.get(RD_FEED).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as c:
        assert await fetch_reddit(RD_URL, client=c) is None
        assert await fetch_reddit(RD_URL, client=c) is None
    assert route.call_count == 1  # second read skipped the feed during the cool-down


@respx.mock
async def test_ladder_reads_reddit_from_feed_before_the_page(_reddit_fresh):
    page = respx.get(RD_URL).mock(return_value=httpx.Response(200, text="<html>shell</html>"))
    respx.get(RD_FEED).mock(return_value=httpx.Response(
        200, text=RD_ATOM, headers={"content-type": "application/atom+xml"}))
    async with httpx.AsyncClient() as c:
        res = await fetch(RD_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "api" and page.call_count == 0


@respx.mock
async def test_ladder_falls_back_to_page_when_feed_fails(_reddit_fresh):
    respx.get(RD_FEED).mock(return_value=httpx.Response(500))
    respx.get(RD_URL).mock(return_value=httpx.Response(
        200, text="<html><body><article>" + "thread text " * 400 + "</article></body></html>"))
    async with httpx.AsyncClient() as c:
        res = await fetch(RD_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "static"
