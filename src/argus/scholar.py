"""The `scholar_search` MCP tool - structured academic-paper search.

Three free public APIs, queried in order via the SSRF-safe client (all are public
fixed hosts -> they pass the guard):

* Primary: **Semantic Scholar Graph API** - richest metadata (citations, abstract,
  open-access PDF). Often 429s anonymously; an optional ``SEMANTIC_SCHOLAR_API_KEY``
  / ``ARGUS_S2_API_KEY`` env raises the rate limit via an ``x-api-key`` header.
* Second: **OpenAlex** - answers where S2 429s. Keyless use gets a tenth of the
  free daily budget (~100 searches/day); an optional ``ARGUS_OPENALEX_API_KEY`` sent as
  a bearer token raises it to ~1,000. The old ``mailto`` polite pool is gone (Feb 2026).
* Last: **CrossRef** - used when both fail or come back empty. The polite pool wants a
  ``mailto`` in the ``User-Agent``.

All return one lean, mapped shape (never raw backend JSON). See docs/03-TOOL-SPECS.md.
"""

import asyncio
import math
import os
import re

import httpx

from argus.security.ssrf import build_safe_async_client

S2_BASE = "https://api.semanticscholar.org"
CROSSREF_BASE = "https://api.crossref.org"
OPENALEX_BASE = "https://api.openalex.org"

_USER_AGENT = "ArgusBot/0.1"
_CROSSREF_UA = "ArgusBot/0.1 (+https://suriota.com; mailto:research@suriota.com)"
_S2_FIELDS = "title,authors,year,venue,citationCount,externalIds,abstract,url,openAccessPdf"
_OA_SELECT = (
    "id,display_name,authorships,publication_year,primary_location,cited_by_count,"
    "doi,abstract_inverted_index,best_oa_location"
)
_TIMEOUT = 20.0
_MAX_LIMIT = 100
_OA_MAX_LIMIT = 50
# One retry only: OpenAlex now catches the 429s, so a long S2 backoff chain just adds
# latency (the old 1+2+4 s chain put p99 at 10.6 s before any fallback ran).
_S2_MAX_RETRIES = 1
# Even a keyed S2 caps at 1 req/s and 429s well below that in practice (measured ~50%
# rejects at 6 s spacing), so never retry sooner than the documented floor.
_S2_BACKOFF_BASE = 1.0
_LOG_CIT_W = 0.1     # weight for containment*log_cit boost in _rerank_results

# Strip JATS / XML tags from CrossRef abstracts (e.g. <jats:p>, <jats:italic>).
_TAG_RE = re.compile(r"<[^>]+>")

# Tokenizer for relevance rerank: lowercase alphanum tokens >= 2 chars (same as argus.search).
_TOKEN_RE = re.compile(r"[a-z0-9]+")

class ScholarError(Exception):
    """Structured failure. ``code`` in {search_backend_down, no_results}."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

def _s2_key() -> str | None:
    """The Semantic Scholar API key from env (ARGUS_S2_API_KEY preferred)."""
    return os.environ.get("ARGUS_S2_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")

def _headers(backend: str) -> dict:
    """Per-backend request headers. UA always; CrossRef UA carries a mailto; S2 adds
    ``x-api-key`` and OpenAlex a bearer token iff their key env is set."""
    if backend == "crossref":
        return {"User-Agent": _CROSSREF_UA}
    if backend == "openalex":
        headers = {"User-Agent": _USER_AGENT}
        # Header, not the api_key query param, so the key never lands in URLs or logs.
        if key := os.environ.get("ARGUS_OPENALEX_API_KEY"):
            headers["Authorization"] = f"Bearer {key}"
        return headers
    headers = {"User-Agent": _USER_AGENT}
    key = _s2_key()
    if key:
        headers["x-api-key"] = key
    return headers

def _map_s2(paper: dict) -> dict:
    pdf = paper.get("openAccessPdf") or {}
    return {
        "title": paper.get("title"),
        "authors": [a.get("name", "") for a in (paper.get("authors") or [])],
        "year": paper.get("year"),
        "venue": paper.get("venue"),
        "citations": paper.get("citationCount"),
        "doi": (paper.get("externalIds") or {}).get("DOI"),
        "url": paper.get("url"),
        "abstract": paper.get("abstract"),
        "open_access_pdf": pdf.get("url"),
    }

def _cr_author(a: dict) -> str:
    return f"{a.get('given', '')} {a.get('family', '')}".strip()

def _cr_year(work: dict) -> int | None:
    parts = ((work.get("published") or {}).get("date-parts") or [[]])[0]
    return parts[0] if parts else None

def _cr_abstract(work: dict) -> str | None:
    raw = work.get("abstract")
    return _TAG_RE.sub("", raw).strip() if raw else None

def _first(seq) -> str | None:
    return seq[0] if seq else None

def _cr_pdf(work: dict) -> str | None:
    """OA PDF URL from CrossRef's ``link`` array (first application/pdf full-text link).

    CrossRef exposes open-access full text via ``link`` entries carrying
    ``content-type``; S2 has ``openAccessPdf.url`` but CrossRef's mapping hardcoded
    None, so ``open_access=True`` dropped every CrossRef-fallback result (the common
    anonymous-S2-429 path). Guard the URL so a malformed payload can't inject a non-URL.
    """
    for link in work.get("link") or []:
        if link.get("content-type") == "application/pdf":
            url = link.get("URL")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
    return None

def _map_crossref(work: dict) -> dict:
    return {
        "title": _first(work.get("title")),
        "authors": [_cr_author(a) for a in (work.get("author") or [])],
        "year": _cr_year(work),
        "venue": _first(work.get("container-title")),
        "citations": work.get("is-referenced-by-count"),
        "doi": work.get("DOI"),
        "url": work.get("URL"),
        "abstract": _cr_abstract(work),
        "open_access_pdf": _cr_pdf(work),
    }

def _oa_abstract(index: dict | None) -> str | None:
    """Rebuild plain text from OpenAlex's ``abstract_inverted_index`` (word -> positions)."""
    if not index:
        return None
    words = {pos: word for word, positions in index.items() for pos in positions}
    return " ".join(words[i] for i in sorted(words))

def _map_openalex(work: dict) -> dict:
    loc = work.get("primary_location") or {}
    doi = work.get("doi")
    pdf = (work.get("best_oa_location") or {}).get("pdf_url")
    return {
        "title": work.get("display_name"),
        "authors": [
            (a.get("author") or {}).get("display_name", "")
            for a in (work.get("authorships") or [])
        ],
        "year": work.get("publication_year"),
        "venue": (loc.get("source") or {}).get("display_name") or loc.get("raw_source_name"),
        "citations": work.get("cited_by_count"),
        # OpenAlex gives the DOI as a URL; the other backends give the bare DOI.
        "doi": doi.removeprefix("https://doi.org/") if doi else None,
        "url": loc.get("landing_page_url") or work.get("id"),
        "abstract": _oa_abstract(work.get("abstract_inverted_index")),
        "open_access_pdf": (
            pdf if isinstance(pdf, str) and pdf.startswith(("http://", "https://")) else None
        ),
    }

def _apply_filters(results: list[dict], year_from: int | None, open_access: bool) -> list[dict]:
    if year_from is not None:
        results = [r for r in results if r["year"] is not None and r["year"] >= year_from]
    if open_access:
        results = [r for r in results if r["open_access_pdf"]]
    return results

def _tokens(text: str) -> set[str]:
    """Lowercase alphanum tokens >= 2 chars (mirrors argus.search._tokens)."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2}

def _rerank_results(query: str, results: list[dict]) -> list[dict]:
    """Sort results by a blended relevance score, descending; ties broken by citations.

    Score = overlap + containment * log10(1 + max(citations, 0)) * _LOG_CIT_W

    where:
      overlap    = |query_tokens & title_tokens| / |query_tokens|
                   (query coverage: how much of the query appears in the title)
      containment = |query_tokens & title_tokens| / |title_tokens|
                   (title precision: fraction of title tokens that are in the query;
                    a short canonical title fully covered by the query scores 1.0,
                    a verbose derivative with extra tokens scores lower)
      The product containment * log_cit rewards titles that are BOTH a tight match
      to the query AND highly cited, while giving zero boost to verbose zero-citation
      derivatives even when their raw overlap fraction is higher.

    Citations None is treated as -1 (effective_cit) so it sorts below any paper with
    >= 0 citations when the blended score is equal. log10 uses max(effective_cit, 0)
    to avoid log(0); the None-vs-0 tiebreak is resolved by the secondary tuple element.
    Original order is preserved on full ties (Python sort is stable).
    """
    qtokens = _tokens(query)
    if not qtokens:
        return results

    def _key(r: dict) -> tuple[float, int]:
        ttokens = _tokens(r.get("title") or "")
        inter = len(qtokens & ttokens)
        overlap = inter / len(qtokens)
        containment = inter / len(ttokens) if ttokens else 0.0
        effective_cit = r["citations"] if r["citations"] is not None else -1
        log_cit = math.log10(1 + max(effective_cit, 0))
        score = overlap + containment * log_cit * _LOG_CIT_W
        return (-score, -effective_cit)

    return sorted(results, key=_key)

async def _try_s2(client, base, query, limit, year_from, open_access):
    """Return mapped+filtered S2 results, or None on any failure (caller falls back).

    On HTTP 429 specifically, retries up to _S2_MAX_RETRIES times with exponential
    backoff (asyncio.sleep(_S2_BACKOFF_BASE * 2**attempt)).  All other non-2xx or transport errors
    return None immediately without retrying.
    """
    params = {"query": query, "limit": limit, "fields": _S2_FIELDS}
    # Filter server-side so `limit` counts matching papers; _apply_filters stays as the net.
    if year_from is not None:
        params["year"] = f"{year_from}-"
    if open_access:
        params["openAccessPdf"] = ""  # S2 treats it as a valueless presence flag
    attempt = 0
    while True:
        try:
            resp = await client.get(
                f"{base}/graph/v1/paper/search", params=params, headers=_headers("s2")
            )
            if resp.status_code == 429:
                if attempt < _S2_MAX_RETRIES:
                    await asyncio.sleep(_S2_BACKOFF_BASE * 2**attempt)
                    attempt += 1
                    continue
                return None
            if resp.status_code < 200 or resp.status_code >= 300:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        papers = data.get("data") or []
        return _apply_filters([_map_s2(p) for p in papers], year_from, open_access)

async def _try_openalex(client, base, query, limit, year_from, open_access):
    """Return mapped+filtered OpenAlex results, or None on any failure."""
    params = {"search": query, "per_page": min(limit, _OA_MAX_LIMIT), "select": _OA_SELECT}
    filters = [f"from_publication_date:{year_from}-01-01"] if year_from is not None else []
    if open_access:
        filters.append("is_oa:true")
    if filters:
        params["filter"] = ",".join(filters)
    try:
        resp = await client.get(f"{base}/works", params=params, headers=_headers("openalex"))
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        # Mapping inside the try: a malformed body must fall through to CrossRef, not
        # escape as an exception that skips the last source.
        works = resp.json().get("results") or []
        return _apply_filters([_map_openalex(w) for w in works], year_from, open_access)
    except (httpx.HTTPError, ValueError, AttributeError, TypeError, KeyError):
        return None

async def _try_crossref(client, base, query, limit, year_from, open_access):
    """Return mapped+filtered CrossRef results, or None on any failure."""
    params = {"query": query, "rows": limit}
    filters = [f"from-pub-date:{year_from}"] if year_from is not None else []
    if open_access:
        filters.append("has-full-text:true")
    if filters:
        params["filter"] = ",".join(filters)
    try:
        resp = await client.get(
            f"{base}/works", params=params, headers=_headers("crossref")
        )
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    items = (data.get("message") or {}).get("items") or []
    return _apply_filters([_map_crossref(w) for w in items], year_from, open_access)

async def scholar_search(
    query: str,
    limit: int = 10,
    year_from: int | None = None,
    open_access: bool = False,
    *,
    client: "httpx.AsyncClient | None" = None,
    s2_base: str = S2_BASE,
    crossref_base: str = CROSSREF_BASE,
    openalex_base: str = OPENALEX_BASE,
) -> dict:
    """Structured academic-paper search.

    Tries Semantic Scholar, then OpenAlex, then CrossRef; each later backend runs only
    when the earlier ones failed / 429ed / came back empty. ``year_from`` drops papers
    older than that year; ``open_access`` keeps only items that carry an open-access PDF.
    Both are sent to each backend as filters and re-applied client-side. ``limit`` is
    capped at 100 (50 for OpenAlex).

    S2 HTTP 429 is retried up to _S2_MAX_RETRIES times with exponential backoff before
    falling back.  Results are relevance-reranked by query/title token-overlap
    fraction (desc) then citations (desc) before returning.

    Returns ``{query, source, results, count}`` where ``source`` is
    ``'semantic_scholar'``, ``'openalex'`` or ``'crossref'`` and each result is::

        {title, authors: [str], year, venue, citations, doi, url, abstract, open_access_pdf}

    All backends empty -> ``ScholarError('no_results')``. ``search_backend_down`` is raised
    only when ALL backends hard-errored (HTTP non-2xx / transport / non-JSON). A backend
    that returns a valid-but-empty page counts as a "soft zero", so e.g. S2-empty +
    the rest erroring is treated as ``no_results``.

    The injected ``client`` is used as-is; if ``None`` an SSRF-safe client is built here and
    closed before returning.
    """
    limit = min(max(limit, 1), _MAX_LIMIT)

    owns_client = client is None
    if owns_client:
        client = build_safe_async_client(timeout=_TIMEOUT)

    try:
        s2 = await _try_s2(client, s2_base, query, limit, year_from, open_access)
        if s2:
            ranked = _rerank_results(query, s2)
            return {
                "query": query, "source": "semantic_scholar",
                "results": ranked, "count": len(ranked),
            }

        oa = await _try_openalex(client, openalex_base, query, limit, year_from, open_access)
        if oa:
            ranked = _rerank_results(query, oa)
            return {"query": query, "source": "openalex", "results": ranked, "count": len(ranked)}

        cr = await _try_crossref(client, crossref_base, query, limit, year_from, open_access)
        if cr:
            ranked = _rerank_results(query, cr)
            return {"query": query, "source": "crossref", "results": ranked, "count": len(ranked)}
    finally:
        if owns_client:
            await client.aclose()

    # No backend produced usable results. Only when every one hard-errored (returned None)
    # is it an outage; any valid-but-empty page makes it no_results.
    if s2 is None and oa is None and cr is None:
        raise ScholarError("search_backend_down", "all scholar backends failed")
    raise ScholarError("no_results", f"no academic results for query: {query!r}")
