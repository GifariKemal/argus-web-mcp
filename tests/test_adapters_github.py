"""GitHub issue/PR adapter: the page's React view loses the body and the comments (uv#1 read
as whitespace plus sidebar on 2026-11-23); the REST API has both, rendered by GitHub."""

import httpx
import pytest
import respx

from argus.extract.article import extract_article
from argus.fetch.adapters import fetch_github, github_target
from argus.fetch.core import fetch


@pytest.fixture(autouse=True)
def _dns(public_dns, monkeypatch):
    """The SSRF guard still resolves every hop; make fixture hosts public. No token, no
    cool-down left over from another test."""
    from argus.fetch import adapters

    monkeypatch.delenv("ARGUS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(adapters, "_gh_backoff_until", 0.0)


URL = "https://github.com/astral-sh/uv/issues/1"
API = "https://api.github.com/repos/astral-sh/uv/issues/1"
REPO = "https://api.github.com/repos/astral-sh/uv"


def _public_repo():
    return respx.get(REPO).mock(return_value=httpx.Response(200, json={"private": False}))


def _comment(i, who="bob"):
    return {"id": i, "user": {"login": who}, "created_at": f"2024-01-0{1 + i % 9}T10:00:00Z",
            "body_html": f"<p>comment number {i}</p>"}


ISSUE = {"number": 1, "title": "Add <basic> CI", "state": "closed",
         "user": {"login": "charliermarsh"}, "created_at": "2023-10-02T12:00:00Z",
         "labels": [{"name": "ci"}], "comments": 2,
         "body_html": "<p>We need <strong>GitHub Actions</strong> on every push.</p>"}


@pytest.mark.parametrize(("url", "expected"), [
    (URL, ("astral-sh", "uv", "1")),
    ("https://www.github.com/espressif/esp-idf/pull/14000", ("espressif", "esp-idf", "14000")),
    ("https://github.com/o/r/issues/7/", ("o", "r", "7")),
    ("https://github.com/o/r/issues/7#issuecomment-1", ("o", "r", "7")),
    ("https://github.com/o/r/pull/7/files", None),  # a diff is not the conversation
    ("https://github.com/o/r/pull/7/commits", None),
    ("https://github.com/../user/issues/1", None),  # would leave /repos/ on the API
    ("https://github.com/o/../issues/1", None),
    ("https://github.com/o/r", None),
    ("https://github.com/o/r/issues", None),
    ("https://github.com/o/r/blob/main/README.md", None),
    ("https://github.com.evil.example/o/r/issues/1", None),
    ("ftp://github.com/o/r/issues/1", None),
])
def test_github_target(url, expected):
    assert github_target(url) == expected


@respx.mock
async def test_fetch_github_reads_issue_body_and_comments():
    _public_repo()
    route = respx.get(API).mock(return_value=httpx.Response(200, json=ISSUE))
    respx.get(f"{API}/comments").mock(return_value=httpx.Response(
        200, json=[_comment(1), _comment(2, "carol")]))
    async with httpx.AsyncClient() as c:
        res = await fetch_github(URL, client=c)
    assert route.calls[0].request.headers["accept"] == "application/vnd.github.html+json"
    assert res["render_path"] == "api" and res["status"] == 200 and res["final_url"] == URL
    assert "<title>Add &lt;basic&gt; CI #1</title>" in res["html"]
    text = extract_article(res["html"], url=URL)["content"]
    for want in ("Add <basic> CI", "charliermarsh", "2023-10-02", "closed", "ci",
                 "GitHub Actions", "comment number 1", "carol", "comment number 2"):
        assert want in text
    assert res["structured"] == {"repo": "astral-sh/uv", "number": 1, "kind": "issue",
                                 "state": "closed", "comments_total": 2, "comments_shown": 2}


@respx.mock
async def test_fetch_github_pages_comments_and_states_the_cap(monkeypatch):
    from argus.fetch import adapters

    monkeypatch.setattr(adapters, "_GH_MAX_COMMENT_PAGES", 2)
    _public_repo()
    respx.get(API).mock(return_value=httpx.Response(
        200, json={**ISSUE, "comments": 250, "pull_request": {"url": "x"}}))
    pages = respx.get(f"{API}/comments").mock(side_effect=[
        httpx.Response(200, json=[_comment(i) for i in range(100)]),
        httpx.Response(200, json=[_comment(i) for i in range(100, 200)]),
    ])
    async with httpx.AsyncClient() as c:
        res = await fetch_github(URL, client=c)
    assert [r.request.url.params["page"] for r in pages.calls] == ["1", "2"]
    assert "comment number 199" in res["html"]
    assert "50 more comments not shown" in res["html"]
    s = res["structured"]
    assert (s["kind"], s["comments_total"], s["comments_shown"]) == ("pull_request", 250, 200)
    assert "review comments" in s["scope"]  # inline review threads are not included


@respx.mock
async def test_fetch_github_sends_the_token_without_following_redirects(monkeypatch):
    monkeypatch.setenv("ARGUS_GITHUB_TOKEN", "tok123")
    route = respx.get(REPO).mock(return_value=httpx.Response(
        301, headers={"location": "https://evil.example/steal"}))
    async with httpx.AsyncClient() as c:
        assert await fetch_github(URL, client=c) is None
    assert route.calls[0].request.headers["authorization"] == "Bearer tok123"
    assert len(respx.calls) == 1  # the credentialed request never follows the redirect


@respx.mock
async def test_fetch_github_degrades_to_none_on_api_failure():
    _public_repo()
    respx.get(API).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    async with httpx.AsyncClient() as c:
        assert await fetch_github(URL, client=c) is None


@respx.mock
async def test_ladder_reads_github_issue_from_api_before_the_page():
    _public_repo()
    respx.get(API).mock(return_value=httpx.Response(200, json={**ISSUE, "comments": 0}))
    respx.get(f"{API}/comments").mock(return_value=httpx.Response(200, json=[]))
    page = respx.get(URL).mock(return_value=httpx.Response(200, text="<html>shell</html>"))
    async with httpx.AsyncClient() as c:
        res = await fetch(URL, client=c)
    assert res["render_path"] == "api" and not page.called


@respx.mock
async def test_fetch_github_never_serves_a_private_repository():
    """The production token could read private repos (2026-11-24); its issues must not
    reach Argus callers or the shared cache."""
    respx.get(REPO).mock(return_value=httpx.Response(200, json={"private": True}))
    issue = respx.get(API).mock(return_value=httpx.Response(200, json=ISSUE))
    async with httpx.AsyncClient() as c:
        assert await fetch_github(URL, client=c) is None
    assert not issue.called


@respx.mock
async def test_fetch_github_rate_limit_pauses_the_adapter():
    route = respx.get(REPO).mock(return_value=httpx.Response(
        403, json={"message": "API rate limit exceeded"}, headers={"x-ratelimit-remaining": "0"}))
    async with httpx.AsyncClient() as c:
        assert await fetch_github(URL, client=c) is None
        assert await fetch_github(URL, client=c) is None
    assert route.call_count == 1  # the second read skipped the API during the cool-down


@respx.mock
async def test_fetch_github_keeps_the_comment_pages_it_got_when_time_runs_out(monkeypatch):
    import asyncio

    _public_repo()
    respx.get(API).mock(return_value=httpx.Response(200, json={**ISSUE, "comments": 150}))

    async def slow(request):
        if request.url.params["page"] == "2":
            await asyncio.sleep(5)
        return httpx.Response(200, json=[_comment(i) for i in range(100)])

    respx.get(f"{API}/comments").mock(side_effect=slow)
    async with httpx.AsyncClient() as c:
        res = await fetch_github(URL, client=c, timeout=1)
    assert res is not None and res["structured"]["comments_shown"] == 100
    assert "50 more comments not shown" in res["html"]
