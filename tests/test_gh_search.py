"""Tests for the `github_search` tool (offline via respx mocking api.github.com).

GitHub REST Search API client: maps repositories / code / issues responses into a
lean, structured shape; never returns raw GitHub JSON. One @pytest.mark.network test
hits the live anonymous API once.
"""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from argus.gh_search import GH_API, GitHubSearchError, _headers, github_search

# Env vars that may carry a token; cleared/set per test to control auth behaviour.
_TOKEN_ENVS = ("GITHUB_TOKEN", "ARGUS_GITHUB_TOKEN")


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    """Default: no token in the environment (anonymous). Tests opt into a token."""
    for name in _TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)


def _query_of(request):
    return parse_qs(urlsplit(str(request.url)).query)


def _client():
    """A plain (non-SSRF) async client; api.github.com is mocked by respx anyway."""
    return httpx.AsyncClient()


# --------------------------------------------------------------------------- #
# repositories mapping
# --------------------------------------------------------------------------- #
def _repo_item(i, *, stars=100, forks=5):
    return {
        "full_name": f"owner{i}/repo{i}",
        "description": f"desc {i}",
        "html_url": f"https://github.com/owner{i}/repo{i}",
        "stargazers_count": stars,
        "forks_count": forks,
        "language": "Python",
        "topics": ["mcp", "fastapi"],
        "owner": {"login": f"owner{i}"},
        "updated_at": "2026-06-01T00:00:00Z",
    }


@respx.mock
async def test_repositories_maps_shape():
    route = respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [_repo_item(1, stars=900), _repo_item(2, stars=10)],
            },
        )
    )
    async with _client() as client:
        out = await github_search("fastmcp", mode="repositories", limit=10, client=client)

    assert route.called
    assert out["mode"] == "repositories"
    assert out["query"] == "fastmcp"
    assert out["total_count"] == 2
    assert out["count"] == 2
    first = out["results"][0]
    assert first == {
        "full_name": "owner1/repo1",
        "description": "desc 1",
        "url": "https://github.com/owner1/repo1",
        "stars": 900,
        "forks": 5,
        "language": "Python",
        "topics": ["mcp", "fastapi"],
        "owner": "owner1",
        "updated": "2026-06-01T00:00:00Z",
    }


@respx.mock
async def test_limit_truncates_results():
    items = [_repo_item(i) for i in range(5)]
    respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(200, json={"total_count": 5, "items": items})
    )
    async with _client() as client:
        out = await github_search("x", mode="repositories", limit=2, client=client)
    assert out["count"] == 2
    assert len(out["results"]) == 2


@respx.mock
async def test_language_qualifier_in_q():
    route = respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(200, json={"total_count": 1, "items": [_repo_item(1)]})
    )
    async with _client() as client:
        await github_search("fastmcp", mode="repositories", language="python", client=client)
    q = _query_of(route.calls.last.request)["q"][0]
    assert "fastmcp" in q
    assert "language:python" in q


@respx.mock
async def test_sort_order_passed_through_when_set():
    route = respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(200, json={"total_count": 1, "items": [_repo_item(1)]})
    )
    async with _client() as client:
        await github_search(
            "x", mode="repositories", sort="stars", order="asc", client=client
        )
    params = _query_of(route.calls.last.request)
    assert params["sort"] == ["stars"]
    assert params["order"] == ["asc"]


@respx.mock
async def test_sort_omitted_when_none():
    route = respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(200, json={"total_count": 1, "items": [_repo_item(1)]})
    )
    async with _client() as client:
        await github_search("x", mode="repositories", client=client)
    params = _query_of(route.calls.last.request)
    assert "sort" not in params


# --------------------------------------------------------------------------- #
# code mode + auth requirement
# --------------------------------------------------------------------------- #
@respx.mock
async def test_code_without_token_raises_no_request(monkeypatch):
    route = respx.get(f"{GH_API}/search/code").mock(
        return_value=httpx.Response(200, json={"total_count": 0, "items": []})
    )
    async with _client() as client:
        with pytest.raises(GitHubSearchError) as exc:
            await github_search("def main", mode="code", client=client)
    assert exc.value.code == "schema_invalid"
    assert not route.called


@respx.mock
async def test_code_with_token_maps_shape(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    route = respx.get(f"{GH_API}/search/code").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "items": [
                    {
                        "name": "main.py",
                        "path": "src/main.py",
                        "html_url": "https://github.com/o/r/blob/main/src/main.py",
                        "repository": {"full_name": "o/r"},
                    }
                ],
            },
        )
    )
    async with _client() as client:
        out = await github_search("def main", mode="code", client=client)
    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer ghp_test123"
    assert out["mode"] == "code"
    assert out["results"][0] == {
        "repo": "o/r",
        "path": "src/main.py",
        "name": "main.py",
        "url": "https://github.com/o/r/blob/main/src/main.py",
    }


@respx.mock
async def test_code_language_qualifier(monkeypatch):
    monkeypatch.setenv("ARGUS_GITHUB_TOKEN", "tok")
    route = respx.get(f"{GH_API}/search/code").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "items": [
                    {
                        "name": "a.py",
                        "path": "a.py",
                        "html_url": "https://github.com/o/r/blob/main/a.py",
                        "repository": {"full_name": "o/r"},
                    }
                ],
            },
        )
    )
    async with _client() as client:
        await github_search("parse", mode="code", language="python", client=client)
    q = _query_of(route.calls.last.request)["q"][0]
    assert "language:python" in q


# --------------------------------------------------------------------------- #
# issues mapping
# --------------------------------------------------------------------------- #
@respx.mock
async def test_issues_maps_shape_and_pull_request_flag():
    respx.get(f"{GH_API}/search/issues").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [
                    {
                        "title": "Bug: crash",
                        "html_url": "https://github.com/o/r/issues/7",
                        "repository_url": "https://api.github.com/repos/o/r",
                        "number": 7,
                        "state": "open",
                        "comments": 3,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-02-01T00:00:00Z",
                    },
                    {
                        "title": "PR: fix it",
                        "html_url": "https://github.com/o/r/pull/8",
                        "number": 8,
                        "state": "closed",
                        "comments": 0,
                        "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/8"},
                        "created_at": "2026-01-03T00:00:00Z",
                        "updated_at": "2026-02-03T00:00:00Z",
                    },
                ],
            },
        )
    )
    async with _client() as client:
        out = await github_search("crash", mode="issues", client=client)

    issue, pr = out["results"]
    assert issue == {
        "title": "Bug: crash",
        "url": "https://github.com/o/r/issues/7",
        "repo": "o/r",
        "number": 7,
        "state": "open",
        "comments": 3,
        "is_pull_request": False,
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-02-01T00:00:00Z",
    }
    assert pr["is_pull_request"] is True
    # repo derived from html_url when repository_url absent.
    assert pr["repo"] == "o/r"
    assert pr["number"] == 8
    assert pr["state"] == "closed"


# --------------------------------------------------------------------------- #
# error paths
# --------------------------------------------------------------------------- #
@respx.mock
async def test_rate_limit_raises_backend_down():
    respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(
            403, headers={"X-RateLimit-Remaining": "0"}, json={"message": "rate limited"}
        )
    )
    async with _client() as client:
        with pytest.raises(GitHubSearchError) as exc:
            await github_search("x", mode="repositories", client=client)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_401_raises_backend_down():
    respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(401, json={"message": "bad creds"})
    )
    async with _client() as client:
        with pytest.raises(GitHubSearchError) as exc:
            await github_search("x", mode="repositories", client=client)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_other_non_2xx_raises_backend_down():
    respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )
    async with _client() as client:
        with pytest.raises(GitHubSearchError) as exc:
            await github_search("x", mode="repositories", client=client)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_empty_results_raises_no_results():
    respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(200, json={"total_count": 0, "items": []})
    )
    async with _client() as client:
        with pytest.raises(GitHubSearchError) as exc:
            await github_search("zzzznope", mode="repositories", client=client)
    assert exc.value.code == "no_results"


@respx.mock
async def test_transport_error_raises_backend_down():
    respx.get(f"{GH_API}/search/repositories").mock(
        side_effect=httpx.ConnectError("boom")
    )
    async with _client() as client:
        with pytest.raises(GitHubSearchError) as exc:
            await github_search("x", mode="repositories", client=client)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_non_json_body_raises_backend_down():
    respx.get(f"{GH_API}/search/repositories").mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    async with _client() as client:
        with pytest.raises(GitHubSearchError) as exc:
            await github_search("x", mode="repositories", client=client)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_bad_mode_raises_schema_invalid_no_request():
    route = respx.get(url__regex=rf"{GH_API}/search/.*").mock(
        return_value=httpx.Response(200, json={"total_count": 0, "items": []})
    )
    async with _client() as client:
        with pytest.raises(GitHubSearchError) as exc:
            await github_search("x", mode="users", client=client)
    assert exc.value.code == "schema_invalid"
    assert not route.called


# --------------------------------------------------------------------------- #
# headers
# --------------------------------------------------------------------------- #
def test_headers_always_have_user_agent_and_accept():
    h = _headers()
    assert h["User-Agent"] == "ArgusBot/0.1"
    assert h["Accept"] == "application/vnd.github+json"
    assert h["X-GitHub-Api-Version"] == "2022-11-28"
    assert "Authorization" not in h


def test_headers_authorization_when_token_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
    assert _headers()["Authorization"] == "Bearer ghp_abc"


def test_headers_authorization_prefers_argus_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ARGUS_GITHUB_TOKEN", "argus_tok")
    assert _headers()["Authorization"] == "Bearer argus_tok"


# --------------------------------------------------------------------------- #
# client lifecycle: builds + closes its own SSRF client when none injected
# --------------------------------------------------------------------------- #
@respx.mock
async def test_builds_and_closes_own_client(monkeypatch):
    # The SSRF-safe client resolves the host and pins the connection to the IP, so
    # rewrite api.github.com -> a fixed public IP and mock that IP host (respx sees the
    # rewritten request). This proves github_search builds AND closes its own client.
    import socket

    pinned = "140.82.112.6"  # a public github.com-range IP; passes the SSRF guard.

    def _gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (pinned, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    respx.get(f"https://{pinned}/search/repositories").mock(
        return_value=httpx.Response(200, json={"total_count": 1, "items": [_repo_item(1)]})
    )
    closed = {}
    real_build = github_search.__globals__["build_safe_async_client"]

    def _spy_build(**kwargs):
        c = real_build(**kwargs)
        orig_aclose = c.aclose

        async def _aclose():
            closed["yes"] = True
            await orig_aclose()

        c.aclose = _aclose
        return c

    monkeypatch.setitem(github_search.__globals__, "build_safe_async_client", _spy_build)
    out = await github_search("fastmcp", mode="repositories")
    assert out["count"] == 1
    assert closed.get("yes") is True


# --------------------------------------------------------------------------- #
# live network test (run once)
# --------------------------------------------------------------------------- #
@pytest.mark.network
async def test_live_anonymous_repositories_search():
    try:
        out = await github_search("fastmcp language:python", mode="repositories", limit=3)
    except GitHubSearchError as exc:
        # Anonymous search is 10 req/min; if rate-limited that day, accept it.
        assert exc.code == "search_backend_down"
        pytest.skip(f"GitHub anon rate-limited: {exc}")
    assert out["count"] >= 1
    assert all("full_name" in r and "stars" in r for r in out["results"])
