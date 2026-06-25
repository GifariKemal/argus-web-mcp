"""The `scholar_search` MCP tool - structured academic-paper search.

Two FREE, key-less public APIs, queried via the SSRF-safe client (both hosts are
public fixed hosts -> they pass the guard):

* Primary: **Semantic Scholar Graph API** - richest metadata (citations, abstract,
  open-access PDF). Often 429s anonymously; an optional ``SEMANTIC_SCHOLAR_API_KEY``
  / ``ARGUS_S2_API_KEY`` env raises the rate limit via an ``x-api-key`` header.
* Fallback: **CrossRef** - used on any S2 failure / 429 / empty result. The polite
  pool wants a ``mailto`` in the ``User-Agent``.

Both return one lean, mapped shape (never raw backend JSON). See docs/03-TOOL-SPECS.md.
"""

import asyncio
import math
import os
import re

import httpx

from argus.security.ssrf import build_safe_async_client

S2_BASE = "https://api.semanticscholar.org"
CROSSREF_BASE = "https://api.crossref.org"

_USER_AGENT = "ArgusBot/0.1"
_CROSSREF_UA = "ArgusBot/0.1 (+https://suriota.com; mailto:research@suriota.com)"
_S2_FIELDS = "title,authors,year,venue,citationCount,externalIds,abstract,url,openAccessPdf"
_TIMEOUT = 20.0
_MAX_LIMIT = 100
_S2_MAX_RETRIES = 2  # retry budget for HTTP 429 from S2
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
    ``x-api-key`` iff a key env is set."""
    if backend == "crossref":
        return {"User-Agent": _CROSSREF_UA}
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
        "open_access_pdf": None,
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
    backoff (asyncio.sleep(0.5 * 2**attempt)).  All other non-2xx or transport errors
    return None immediately without retrying.
    """
    params = {"query": query, "limit": limit, "fields": _S2_FIELDS}
    attempt = 0
    while True:
        try:
            resp = await client.get(
                f"{base}/graph/v1/paper/search", params=params, headers=_headers("s2")
            )
            if resp.status_code == 429:
                if attempt < _S2_MAX_RETRIES:
                    await asyncio.sleep(0.5 * 2**attempt)
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

async def _try_crossref(client, base, query, limit, year_from, open_access):
    """Return mapped+filtered CrossRef results, or None on any failure."""
    params = {"query": query, "rows": limit}
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
) -> dict:
    """Structured academic-paper search.

    Tries Semantic Scholar first; on any S2 failure / 429 / empty result, falls back to
    CrossRef. ``year_from`` drops papers older than that year (client-side); ``open_access``
    keeps only items that carry an open-access PDF. ``limit`` is capped at 100.

    S2 HTTP 429 is retried up to _S2_MAX_RETRIES times with exponential backoff before
    falling back to CrossRef.  Results are relevance-reranked by query/title token-overlap
    fraction (desc) then citations (desc) before returning.

    Returns ``{query, source, results, count}`` where ``source`` is
    ``'semantic_scholar'`` or ``'crossref'`` and each result is::

        {title, authors: [str], year, venue, citations, doi, url, abstract, open_access_pdf}

    Both backends empty -> ``ScholarError('no_results')``. ``search_backend_down`` is raised
    only when BOTH backends hard-errored (HTTP non-2xx / transport / non-JSON). A backend
    that returns a valid-but-empty page counts as a "soft zero", so e.g. S2-empty +
    CrossRef-error is treated as ``no_results`` (not both errored).

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

        cr = await _try_crossref(client, crossref_base, query, limit, year_from, open_access)
        if cr:
            ranked = _rerank_results(query, cr)
            return {"query": query, "source": "crossref", "results": ranked, "count": len(ranked)}
    finally:
        if owns_client:
            await client.aclose()

    # Neither backend produced usable results. If at least one hard-errored (returned
    # None), it's a backend outage; if both returned valid-but-empty lists, it's no_results.
    if s2 is None and cr is None:
        raise ScholarError("search_backend_down", "both scholar backends failed")
    raise ScholarError("no_results", f"no academic results for query: {query!r}")
