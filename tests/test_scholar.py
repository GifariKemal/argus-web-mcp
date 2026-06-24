"""Tests for the `scholar_search` tool (offline via respx).

Structured academic-paper search: tries Semantic Scholar Graph API first, falls back
to CrossRef on S2 failure/429/empty, and maps both backends into one lean shape. One
@pytest.mark.network test hits the live free APIs once.
"""

import httpx
import pytest
import respx

from argus.scholar import (
    CROSSREF_BASE,
    S2_BASE,
    ScholarError,
    _headers,
    scholar_search,
)

# Env vars that may carry an S2 key; cleared by default, set per test.
_KEY_ENVS = ("SEMANTIC_SCHOLAR_API_KEY", "ARGUS_S2_API_KEY")

_S2_SEARCH = f"{S2_BASE}/graph/v1/paper/search"
_CR_WORKS = f"{CROSSREF_BASE}/works"


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    """Default: no S2 API key in the environment. Tests opt into a key."""
    for name in _KEY_ENVS:
        monkeypatch.delenv(name, raising=False)


def _client():
    """A plain (non-SSRF) async client; the API hosts are mocked by respx anyway."""
    return httpx.AsyncClient()


# --------------------------------------------------------------------------- #
# Semantic Scholar mapping (primary)
# --------------------------------------------------------------------------- #
def _s2_paper(i, *, year=2020, citations=100, pdf=True):
    return {
        "title": f"Paper {i}",
        "authors": [{"name": f"Author {i}A"}, {"name": f"Author {i}B"}],
        "year": year,
        "venue": f"Venue {i}",
        "citationCount": citations,
        "externalIds": {"DOI": f"10.1000/paper{i}"},
        "abstract": f"Abstract of paper {i}.",
        "url": f"https://www.semanticscholar.org/paper/{i}",
        "openAccessPdf": {"url": f"https://example.org/paper{i}.pdf"} if pdf else None,
    }


@respx.mock
async def test_s2_happy_maps_shape():
    route = respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(
            200, json={"total": 2, "data": [_s2_paper(1, citations=900), _s2_paper(2)]}
        )
    )
    async with _client() as client:
        out = await scholar_search("transformers", limit=10, client=client)

    assert route.called
    assert out["source"] == "semantic_scholar"
    assert out["query"] == "transformers"
    assert out["count"] == 2
    first = out["results"][0]
    assert first == {
        "title": "Paper 1",
        "authors": ["Author 1A", "Author 1B"],
        "year": 2020,
        "venue": "Venue 1",
        "citations": 900,
        "doi": "10.1000/paper1",
        "url": "https://www.semanticscholar.org/paper/1",
        "abstract": "Abstract of paper 1.",
        "open_access_pdf": "https://example.org/paper1.pdf",
    }


@respx.mock
async def test_s2_request_carries_fields_and_limit():
    route = respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(200, json={"data": [_s2_paper(1)]})
    )
    async with _client() as client:
        await scholar_search("x", limit=5, client=client)
    params = dict(route.calls.last.request.url.params)
    assert params["query"] == "x"
    assert params["limit"] == "5"
    assert "title" in params["fields"]
    assert "openAccessPdf" in params["fields"]


# --------------------------------------------------------------------------- #
# CrossRef fallback (S2 429 / failure / empty)
# --------------------------------------------------------------------------- #
def _cr_work(i, *, year=2019, citations=50, abstract=None):
    work = {
        "title": [f"CR Paper {i}"],
        "author": [
            {"given": "Ada", "family": f"Lovelace{i}"},
            {"given": "Alan", "family": "Turing"},
        ],
        "published": {"date-parts": [[year, 6, 1]]},
        "container-title": [f"CR Venue {i}"],
        "is-referenced-by-count": citations,
        "DOI": f"10.5555/cr{i}",
        "URL": f"https://doi.org/10.5555/cr{i}",
    }
    if abstract is not None:
        work["abstract"] = abstract
    return work


@respx.mock
async def test_s2_429_falls_back_to_crossref():
    s2 = respx.get(_S2_SEARCH).mock(return_value=httpx.Response(429, json={"message": "rate"}))
    cr = respx.get(_CR_WORKS).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        _cr_work(
                            1,
                            year=2017,
                            citations=12345,
                            abstract=(
                                "<jats:p>The <jats:italic>dominant</jats:italic> models.</jats:p>"
                            ),
                        ),
                        _cr_work(2),
                    ]
                }
            },
        )
    )
    async with _client() as client:
        out = await scholar_search("attention", limit=10, client=client)

    assert s2.called
    assert cr.called
    assert out["source"] == "crossref"
    assert out["count"] == 2
    first = out["results"][0]
    assert first["title"] == "CR Paper 1"
    assert first["authors"] == ["Ada Lovelace1", "Alan Turing"]
    assert first["year"] == 2017
    assert first["venue"] == "CR Venue 1"
    assert first["citations"] == 12345
    assert first["doi"] == "10.5555/cr1"
    assert first["url"] == "https://doi.org/10.5555/cr1"
    assert first["open_access_pdf"] is None
    # JATS tags stripped from the abstract.
    assert first["abstract"] == "The dominant models."


@respx.mock
async def test_s2_empty_falls_back_to_crossref():
    respx.get(_S2_SEARCH).mock(return_value=httpx.Response(200, json={"data": []}))
    cr = respx.get(_CR_WORKS).mock(
        return_value=httpx.Response(200, json={"message": {"items": [_cr_work(1)]}})
    )
    async with _client() as client:
        out = await scholar_search("x", client=client)
    assert cr.called
    assert out["source"] == "crossref"
    assert out["count"] == 1


# --------------------------------------------------------------------------- #
# error paths
# --------------------------------------------------------------------------- #
@respx.mock
async def test_both_empty_raises_no_results():
    respx.get(_S2_SEARCH).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(_CR_WORKS).mock(
        return_value=httpx.Response(200, json={"message": {"items": []}})
    )
    async with _client() as client:
        with pytest.raises(ScholarError) as exc:
            await scholar_search("zzznope", client=client)
    assert exc.value.code == "no_results"


@respx.mock
async def test_both_error_raises_backend_down():
    respx.get(_S2_SEARCH).mock(return_value=httpx.Response(429, json={"message": "rate"}))
    respx.get(_CR_WORKS).mock(return_value=httpx.Response(500, json={"message": "boom"}))
    async with _client() as client:
        with pytest.raises(ScholarError) as exc:
            await scholar_search("x", client=client)
    assert exc.value.code == "search_backend_down"


@respx.mock
async def test_s2_empty_crossref_error_is_no_results():
    # Contract: backend_down requires BOTH backends to have errored. Here S2 returned a
    # valid-but-empty page (a "soft zero"), so not both errored -> no_results.
    respx.get(_S2_SEARCH).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(_CR_WORKS).mock(return_value=httpx.Response(503, json={"message": "down"}))
    async with _client() as client:
        with pytest.raises(ScholarError) as exc:
            await scholar_search("x", client=client)
    assert exc.value.code == "no_results"


@respx.mock
async def test_s2_transport_error_falls_back():
    respx.get(_S2_SEARCH).mock(side_effect=httpx.ConnectError("boom"))
    cr = respx.get(_CR_WORKS).mock(
        return_value=httpx.Response(200, json={"message": {"items": [_cr_work(1)]}})
    )
    async with _client() as client:
        out = await scholar_search("x", client=client)
    assert cr.called
    assert out["source"] == "crossref"


@respx.mock
async def test_both_transport_error_raises_backend_down():
    # Both backends raise transport errors (the except-branch in each helper) -> backend_down.
    respx.get(_S2_SEARCH).mock(side_effect=httpx.ConnectError("s2 down"))
    respx.get(_CR_WORKS).mock(side_effect=httpx.ConnectError("cr down"))
    async with _client() as client:
        with pytest.raises(ScholarError) as exc:
            await scholar_search("x", client=client)
    assert exc.value.code == "search_backend_down"


# --------------------------------------------------------------------------- #
# client-side filters: year_from + open_access
# --------------------------------------------------------------------------- #
@respx.mock
async def test_year_from_drops_older_papers():
    respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _s2_paper(1, year=2024),
                    _s2_paper(2, year=2010),
                    _s2_paper(3, year=2021),
                ]
            },
        )
    )
    async with _client() as client:
        out = await scholar_search("x", year_from=2021, client=client)
    years = [r["year"] for r in out["results"]]
    assert years == [2024, 2021]
    assert out["count"] == 2


@respx.mock
async def test_open_access_keeps_only_items_with_pdf():
    respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={"data": [_s2_paper(1, pdf=True), _s2_paper(2, pdf=False)]},
        )
    )
    async with _client() as client:
        out = await scholar_search("x", open_access=True, client=client)
    assert out["count"] == 1
    assert out["results"][0]["open_access_pdf"] == "https://example.org/paper1.pdf"


@respx.mock
async def test_filters_emptying_results_raises_no_results():
    # All S2 papers filtered out by year_from, and CrossRef also empty post-filter ->
    # no_results (both backends produced zero usable results).
    respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(200, json={"data": [_s2_paper(1, year=1999)]})
    )
    respx.get(_CR_WORKS).mock(
        return_value=httpx.Response(200, json={"message": {"items": [_cr_work(1, year=1990)]}})
    )
    async with _client() as client:
        with pytest.raises(ScholarError) as exc:
            await scholar_search("x", year_from=2020, client=client)
    assert exc.value.code == "no_results"


# --------------------------------------------------------------------------- #
# headers: S2 key + User-Agent + CrossRef mailto
# --------------------------------------------------------------------------- #
def test_s2_headers_user_agent_no_key():
    h = _headers("s2")
    assert h["User-Agent"]
    assert "x-api-key" not in h


def test_s2_headers_api_key_when_env_set(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2key123")
    assert _headers("s2")["x-api-key"] == "s2key123"


def test_s2_headers_prefers_argus_key(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.setenv("ARGUS_S2_API_KEY", "argusk")
    assert _headers("s2")["x-api-key"] == "argusk"


def test_crossref_headers_user_agent_has_mailto():
    h = _headers("crossref")
    assert "mailto:" in h["User-Agent"]


@respx.mock
async def test_s2_api_key_sent_on_request(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2key123")
    route = respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(200, json={"data": [_s2_paper(1)]})
    )
    async with _client() as client:
        await scholar_search("x", client=client)
    assert route.calls.last.request.headers["x-api-key"] == "s2key123"


# --------------------------------------------------------------------------- #
# limit cap
# --------------------------------------------------------------------------- #
@respx.mock
async def test_limit_capped_at_100():
    route = respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(200, json={"data": [_s2_paper(1)]})
    )
    async with _client() as client:
        await scholar_search("x", limit=500, client=client)
    assert dict(route.calls.last.request.url.params)["limit"] == "100"


# --------------------------------------------------------------------------- #
# client lifecycle: builds + closes its own SSRF client when none injected
# --------------------------------------------------------------------------- #
@respx.mock
async def test_builds_and_closes_own_client(monkeypatch):
    import socket

    pinned = "104.16.0.1"  # public Cloudflare-range IP; passes the SSRF guard.

    def _gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (pinned, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    respx.get(f"https://{pinned}/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json={"data": [_s2_paper(1)]})
    )

    closed = {}
    real_build = scholar_search.__globals__["build_safe_async_client"]

    def _spy_build(**kwargs):
        c = real_build(**kwargs)
        orig_aclose = c.aclose

        async def _aclose():
            closed["yes"] = True
            await orig_aclose()

        c.aclose = _aclose
        return c

    monkeypatch.setitem(scholar_search.__globals__, "build_safe_async_client", _spy_build)
    out = await scholar_search("transformers", limit=3)
    assert out["count"] == 1
    assert closed.get("yes") is True


# --------------------------------------------------------------------------- #
# live network test (run once)
# --------------------------------------------------------------------------- #
@pytest.mark.network
async def test_live_scholar_search():
    try:
        out = await scholar_search("attention is all you need", limit=3)
    except ScholarError as exc:
        # Both free backends rate-limited at that moment - accept but surface.
        assert exc.code == "search_backend_down"
        pytest.skip(f"both scholar backends rate-limited: {exc}")
    assert out["count"] >= 1
    assert out["source"] in ("semantic_scholar", "crossref")
    for r in out["results"]:
        assert r["title"]
        assert r["citations"] is not None or r["doi"] is not None
