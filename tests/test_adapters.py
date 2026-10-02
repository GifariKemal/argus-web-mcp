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
