"""The `github_search` MCP tool - GitHub REST Search API client.

Searches GitHub repositories / code / issues via the public REST Search API and
returns a lean, structured shape (never raw GitHub JSON). See docs/03-TOOL-SPECS.md.

GitHub requires a ``User-Agent`` (omitting it -> 403) and uses content negotiation
(``Accept: application/vnd.github+json`` + ``X-GitHub-Api-Version``). An optional
``GITHUB_TOKEN``/``ARGUS_GITHUB_TOKEN`` env raises the rate limit and is REQUIRED
for code search. api.github.com is public, so it passes the SSRF guard; the SSRF-safe
client is injected by the server (or built+closed here when standalone).
"""

import os
from urllib.parse import urlsplit

import httpx

from argus.security.ssrf import build_safe_async_client

GH_API = "https://api.github.com"
_MODES = {"repositories", "code", "issues"}
_USER_AGENT = "ArgusBot/0.1"
_API_VERSION = "2022-11-28"
_TIMEOUT = 15.0
# Per-mode language qualifier applies to repository/code search (not issues).
_LANGUAGE_MODES = {"repositories", "code"}


class GitHubSearchError(Exception):
    """Structured failure. `code` in {search_backend_down, no_results, schema_invalid}."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _token() -> str | None:
    """The GitHub token from env (ARGUS_GITHUB_TOKEN preferred, then GITHUB_TOKEN)."""
    return os.environ.get("ARGUS_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _headers() -> dict:
    """Accept + User-Agent + X-GitHub-Api-Version always; Authorization iff a token is set."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": _API_VERSION,
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _repo_from_issue(item: dict) -> str:
    """Derive ``owner/repo`` for an issue/PR from repository_url or html_url."""
    repo_url = item.get("repository_url")
    if repo_url:
        # https://api.github.com/repos/{owner}/{repo}
        return repo_url.rsplit("/repos/", 1)[-1]
    # Fall back to the html_url path: /{owner}/{repo}/(issues|pull)/{n}
    parts = urlsplit(item.get("html_url", "")).path.strip("/").split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else ""


def _map_repository(item: dict) -> dict:
    return {
        "full_name": item.get("full_name", ""),
        "description": item.get("description"),
        "url": item.get("html_url", ""),
        "stars": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "language": item.get("language"),
        "topics": item.get("topics", []),
        "owner": (item.get("owner") or {}).get("login", ""),
        "updated": item.get("updated_at"),
    }


def _map_code(item: dict) -> dict:
    return {
        "repo": (item.get("repository") or {}).get("full_name", ""),
        "path": item.get("path", ""),
        "name": item.get("name", ""),
        "url": item.get("html_url", ""),
    }


def _map_issue(item: dict) -> dict:
    return {
        "title": item.get("title", ""),
        "url": item.get("html_url", ""),
        "repo": _repo_from_issue(item),
        "number": item.get("number"),
        "state": item.get("state"),
        "comments": item.get("comments", 0),
        "is_pull_request": "pull_request" in item,
        "created": item.get("created_at"),
        "updated": item.get("updated_at"),
    }


_MAPPERS = {
    "repositories": _map_repository,
    "code": _map_code,
    "issues": _map_issue,
}


def _build_query(query: str, mode: str, language: str | None) -> str:
    """Compose the GitHub ``q`` string: raw query + ``language:<x>`` qualifier when apt."""
    q = query
    if language and mode in _LANGUAGE_MODES:
        q = f"{q} language:{language}"
    return q


async def github_search(
    query: str,
    mode: str = "repositories",
    language: str | None = None,
    sort: str | None = None,
    order: str = "desc",
    limit: int = 10,
    *,
    client: "httpx.AsyncClient | None" = None,
    base_url: str = GH_API,
) -> dict:
    """Search GitHub repositories / code / issues and return a mapped, lean dict.

    ``mode`` must be one of {repositories, code, issues} else ``GitHubSearchError
    ('schema_invalid')``. GitHub CODE search REQUIRES auth, so when ``mode == 'code'``
    and no token env is set this raises ``schema_invalid`` BEFORE any request.

    The ``q`` string is ``query`` plus a ``language:<language>`` qualifier for
    repositories/code when ``language`` is given. ``sort``/``order`` are passed through
    only when ``sort`` is set (otherwise GitHub's best-match relevance is used).
    ``per_page = min(max(limit, 1), 100)``; one request is made and results are mapped
    then truncated to ``limit``.

    Returns ``{query, mode, total_count, results, count}`` - never raw GitHub JSON.

    Errors: a 401, or a 403/429 carrying ``X-RateLimit-Remaining == '0'``, maps to
    ``search_backend_down`` with an auth/rate-limit hint; any other non-2xx also maps to
    ``search_backend_down``. Empty items -> ``no_results``.

    The injected ``client`` is used as-is; if ``None`` an SSRF-safe client is built here
    and closed before returning.
    """
    if mode not in _MODES:
        raise GitHubSearchError(
            "schema_invalid", f"mode must be one of {sorted(_MODES)}, got {mode!r}"
        )
    if mode == "code" and not _token():
        raise GitHubSearchError(
            "schema_invalid", "code search requires GITHUB_TOKEN"
        )

    per_page = min(max(limit, 1), 100)
    params: dict = {"q": _build_query(query, mode, language), "per_page": per_page}
    if sort:
        params["sort"] = sort
        params["order"] = order

    owns_client = client is None
    if owns_client:
        client = build_safe_async_client(timeout=_TIMEOUT)

    try:
        resp = await client.get(
            f"{base_url}/search/{mode}", params=params, headers=_headers()
        )
        if resp.status_code < 200 or resp.status_code >= 300:
            rate_limited = (
                resp.status_code in (403, 429)
                and resp.headers.get("X-RateLimit-Remaining") == "0"
            )
            if rate_limited or resp.status_code == 401:
                raise GitHubSearchError(
                    "search_backend_down",
                    "GitHub rate limit / auth - set GITHUB_TOKEN",
                )
            raise GitHubSearchError(
                "search_backend_down",
                f"GitHub search failed: HTTP {resp.status_code}",
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise GitHubSearchError(
                "search_backend_down", f"GitHub returned non-JSON: {exc}"
            ) from exc
    except httpx.HTTPError as exc:
        raise GitHubSearchError(
            "search_backend_down", f"GitHub request failed: {exc}"
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    items = data.get("items", []) or []
    if not items:
        raise GitHubSearchError("no_results", f"no GitHub {mode} for query: {query!r}")

    mapper = _MAPPERS[mode]
    results = [mapper(item) for item in items[:limit]]
    return {
        "query": query,
        "mode": mode,
        "total_count": data.get("total_count", len(results)),
        "results": results,
        "count": len(results),
    }
