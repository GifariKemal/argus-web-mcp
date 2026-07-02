"""The `search` MCP tool - self-hosted SearXNG JSON API client.

Paginates SearXNG (`pageno=1,2,...`), dedups by URL, maps `content` -> `snippet`,
and returns a lean result dict. See docs/03-TOOL-SPECS.md.
"""

import asyncio
import os
import re
from urllib.parse import urlsplit

import httpx

import argus.semantic as semantic
from argus.router import classify
from argus.security.ssrf import SSRFError, build_safe_async_client, validate_url

_VALID_CATEGORIES = frozenset({"general", "news", "science", "it"})
_VALID_TIME_RANGES = frozenset({"day", "week", "month", "year"})  # SearXNG-accepted values
_MAX_PAGES = 5
_TIMEOUT = 15.0
_CONNECT_TIMEOUT = 2.0  # fast-fail a dead/hung SearXNG instead of hanging _TIMEOUT seconds
_BACKOFF_BASE = 0.5  # seconds; exponential: _BACKOFF_BASE * 2**attempt
# Spread `general` load across many free engines so DuckDuckGo isn't the sole source
# (the 200-scenario benchmark showed ddg answered 189/200 - a single-point risk).
_DEFAULT_ENGINES = ["duckduckgo", "bing", "brave", "mojeek", "startpage", "qwant"]
_DEFAULT_LANG_ENV = "ARGUS_DEFAULT_SEARCH_LANG"
_DEFAULT_LANG = "en"
_MIN_KEEP = 3  # safety floor: never drop below this many of the backend's results
_TITLE_WEIGHT = 2.0  # title-token coverage counts double vs snippet coverage
# Recency boost (rerank v2): a bounded ADDITIVE bump for results carrying a `published`
# date when recency intent is on. Kept a fraction of _TITLE_WEIGHT so RELEVANCE still
# dominates - a fully-irrelevant fresh result (score 0 + 0.5) can never outrank a
# result with any title-token overlap (>= _TITLE_WEIGHT/len(qtokens) which, even for a
# single token in a long query, plus possible snippet, is gated by the overlap floor:
# zero-overlap results are dropped first, so the boost only reorders RELEVANT results).
_RECENCY_BOOST = 0.5
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Hybrid semantic rerank (auto-enabled when argus.semantic.available()).
# Blended score = _SEM_WEIGHT * cosine + (1 - _SEM_WEIGHT) * lexical_norm. Semantic-leaning
# (0.6) so conceptual/paraphrase matches surface, but lexical (0.4) still anchors ranking.
_SEM_WEIGHT = 0.6
# A result with ZERO lexical overlap is NOT hard-dropped under semantic (a paraphrase can be
# rescued), but one with BOTH zero lexical overlap AND cosine < _SEM_FLOOR is clearly
# irrelevant and dropped (subject to the _MIN_KEEP safety floor).
_SEM_FLOOR = 0.3
_SEM_GUARD_FLOOR = 0.55
# Relative relevance gate (v3): after the zero-overlap / SEM_FLOOR drop, apply a gentle
# relative floor: drop a kept result only if BOTH (a) its score < _REL_FLOOR * top_score
# AND (b) keeping the drop still leaves at least _MIN_KEEP results. This trims "single
# generic token in a long query" backfill (e.g. Docker Hub cert-manager surviving only
# because of the token "manager" in a 5-token query) without touching legitimately
# diverse result sets where all results have similar scores.
_REL_FLOOR = 0.25
# Docker Hub host-penalty (F3): de-prioritise hub.docker.com results when the query has no
# container intent.  A MULTIPLICATIVE factor is applied to the raw score before the
# relative-relevance gate, composing cleanly with _REL_FLOOR without changing the gate
# itself.  Only fires when the query contains NONE of _DOCKER_INTENT_TOKENS (conservative).
_DOCKER_HUB_DOMAIN = "hub.docker.com"
_DOCKER_PENALTY = 0.4  # multiply raw score by this; pulls ratio below _REL_FLOOR (0.25)
_DOCKER_INTENT_TOKENS = frozenset(
    {"docker", "container", "containers", "image", "dockerfile",
     "compose", "kubernetes", "k8s", "registry", "oci"}
)
# Generic-token down-weight: a SMALL fixed stoplist of very-common, low-information
# query tokens. A result whose ONLY query-token overlap is generic tokens (i.e. it
# matches NO content-bearing query token) gets its score scaled by _GENERIC_WEIGHT, so a
# lone "manager"/"guide"-style match can't inflate an otherwise-irrelevant result.
# Deliberately conservative: NOT IDF (no corpus); the penalty applies ONLY to
# generic-ONLY matches - any content-token match leaves the score untouched, so a
# legitimately relevant result is never demoted and the (equal-weight) relevance proxy
# can't regress.  Multiplicative, composing with the Docker host-penalty and the
# relative-relevance gate.
_GENERIC_TOKENS = frozenset(
    {"guide", "best", "how", "top", "manager", "component",
     "tutorial", "example", "overview", "introduction", "fast", "fastest",
     "way", "difference", "explained", "simply", "status", "comparison"}
)
_GENERIC_WEIGHT = 0.5  # multiply score by this when ALL matched query tokens are generic
# Stopwords excluded from the RELEVANCE-GUARD overlap check only (never from rerank
# scoring): a natural-language query ("how to install the hermes agent") shares
# 'to'/'the'/'install' with almost any English filler page, which let a garbage result
# set masquerade as majority-relevant. 1-char words are already dropped by _tokens.
_STOPWORDS = frozenset(
    {"the", "to", "of", "in", "on", "for", "and", "or", "is", "are", "be", "an",
     "with", "at", "by", "from", "as", "it", "this", "that", "what", "which",
     "how", "do", "does", "can"}
)
# Tracking params stripped for URL dedup; every other query param is MEANINGFUL
# (?v=, ?id=, ?p= key distinct pages and must not collapse into one).
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "msclkid", "srsltid", "ref", "ref_src", "spm"})


class SearchError(Exception):
    """Structured search failure. `code` in {search_backend_down, no_results}."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _map_result(raw: dict) -> dict:
    out = {
        "title": raw.get("title", ""),
        "url": raw.get("url", ""),
        "snippet": raw.get("content", ""),
        "engine": raw.get("engine", ""),
    }
    published = raw.get("publishedDate")
    if published:
        out["published"] = published
    return out


def _tokens(text: str) -> set[str]:
    """Lowercase, strip punctuation, split hyphens/whitespace; keep tokens >=2 chars."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2}


def _is_tracking_param(param: str) -> bool:
    name = param.split("=")[0].lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def _norm_url(url: str) -> str:
    """Normalize for dedup: drop scheme, fragment, trailing slash, and tracking params.

    Meaningful query params are KEPT (sorted, so param-order permutations still dedup)
    - two watch?v= / thread?id= pages are distinct resources, not duplicates. Param
    values stay case-sensitive (?v=AAA vs ?v=aaa differ); only host/path lowercase.
    """
    parts = urlsplit(url)
    base = f"{parts.netloc}{parts.path}".rstrip("/").lower()
    qs = "&".join(sorted(p for p in parts.query.split("&") if p and not _is_tracking_param(p)))
    return f"{base}?{qs}" if qs else base


def _norm_title(title: str) -> str:
    """Normalize for dedup: lowercase, collapse internal whitespace."""
    return " ".join(title.lower().split())


def _host(url: str) -> str:
    """Lowercased network host (no port) for domain matching."""
    return urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()


def _host_matches(host: str, domain: str) -> bool:
    """Label-boundary suffix match: ``example.com`` matches ``www.example.com`` and
    ``example.com`` itself, but NOT ``notexample.com`` nor ``github.io`` for
    ``github.com``. Matches iff host == domain OR host ends with ``.`` + domain.
    """
    domain = domain.strip().lower().lstrip(".")
    if not domain or not host:
        return False
    return host == domain or host.endswith("." + domain)


def _filter_domains(
    results: list[dict],
    include: list[str] | None,
    exclude: list[str] | None,
) -> list[dict]:
    """Post-filter mapped results by registrable host (suffix match on label boundary).

    ``include`` keeps only results whose host matches one of the include domains;
    ``exclude`` drops results whose host matches one of the exclude domains. Exclude
    is applied after include. Applied BEFORE rerank/``[:count]``.
    """
    out = results
    if include:
        out = [r for r in out
               if any(_host_matches(_host(r.get("url", "")), d) for d in include)]
    if exclude:
        out = [r for r in out
               if not any(_host_matches(_host(r.get("url", "")), d) for d in exclude)]
    return out


def _rel_floor(kept: list, *, key) -> list:
    """Relative relevance gate (v3): drop backfill far below the best result.

    Iterates kept (already sorted descending by key(item)) and drops an item
    only when BOTH conditions hold:
      (a) key(item) < _REL_FLOOR * top_score
      (b) dropping it still leaves at least _MIN_KEEP items.

    top_score is key(kept[0]).  A zero top_score (all items scored 0, which
    only happens when no query tokens exist and the caller already returned early) is
    treated as a no-op to avoid division-by-zero.  Never drops below _MIN_KEEP.
    """
    if len(kept) <= _MIN_KEEP:
        return kept
    top_score = key(kept[0])
    if top_score <= 0:
        return kept
    floor = _REL_FLOOR * top_score
    result = []
    remaining = len(kept)
    for item in kept:
        if key(item) < floor and remaining - 1 >= _MIN_KEEP:
            remaining -= 1
        else:
            result.append(item)
    return result


def rerank(
    query: str,
    results: list[dict],
    recency: bool = False,
    semantic_rerank: bool | None = None,
) -> list[dict]:
    """Deterministic relevance rerank + dedup of mapped results.

    Hybrid semantic rerank (``semantic_rerank``): ``None`` auto-enables it iff
    ``semantic.available()`` is True; ``True``/``False`` force it on/off. When ON, each
    kept result gets a cosine similarity ``sem`` (0..1) of the query vs ``title+snippet``;
    the lexical score is min-max normalized to ``lex_norm`` (0..1, div0-guarded) and the
    final blended score is ``_SEM_WEIGHT*sem + (1-_SEM_WEIGHT)*lex_norm`` (semantic 0.6 /
    lexical 0.4). Zero-lexical-overlap results are NOT hard-dropped (a paraphrase can be
    rescued by semantics) UNLESS their cosine is also below ``_SEM_FLOOR`` (clearly
    irrelevant). If ``semantic.similarities`` raises, the failure is caught and the rerank
    falls back to the pure-lexical behavior below (search never fails because of semantics).

    Each result is ``{title, url, snippet, engine, ...}``.

    Scoring: tokenize ``query`` into a distinct set of lowercased word tokens
    (>=2 chars, punctuation stripped, hyphens split so ``esp-claw`` -> ``{esp, claw}``).
    For every result, ``score = TITLE_WEIGHT * title_coverage + snippet_coverage``,
    where ``*_coverage`` is the fraction of distinct query tokens present in that
    field. Title matches therefore outweigh snippet matches.

    Dedup: later duplicates are dropped by normalized URL (scheme/query/fragment/
    trailing-slash stripped) and by normalized title (case + whitespace collapsed).

    Filtering: results with ZERO query-token overlap in BOTH title and snippet are
    dropped - UNLESS that would leave fewer than ``_MIN_KEEP`` results, in which
    case the backend's original top results are kept (never empty given input).

    Recency tiebreak (always on): among results with EQUAL score, one carrying a
    ``published`` date sorts before one without. This is only a tiebreak.

    Recency boost (v2, ``recency=True``): results carrying a ``published`` date get a
    bounded ADDITIVE score bump of ``+_RECENCY_BOOST``. It is a *boost, not an override*:
    the boost is applied to the score AFTER zero-overlap (irrelevant) results have been
    dropped, so a fully-irrelevant fresh result is removed before any boost and can never
    outrank a relevant stale one. Because ``_RECENCY_BOOST`` (0.5) is a fraction of
    ``_TITLE_WEIGHT`` (2.0), relevance still dominates among kept results.
    """
    if not results:
        return []

    qtokens = _tokens(query)
    # Pre-compute once: True when the query has no container/docker intent tokens.
    _no_docker_intent = not (qtokens & _DOCKER_INTENT_TOKENS)

    # Dedup (belt-and-suspenders over search()'s url dedup) + score, preserving order.
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    # tuple: (score, fresh_rank, has_overlap, idx, result); fresh_rank 0=has published.
    scored: list[tuple[float, int, bool, int, dict]] = []
    for idx, r in enumerate(results):
        nurl = _norm_url(r.get("url", ""))
        ntitle = _norm_title(r.get("title", ""))
        if (nurl and nurl in seen_urls) or (ntitle and ntitle in seen_titles):
            continue
        if nurl:
            seen_urls.add(nurl)
        if ntitle:
            seen_titles.add(ntitle)

        title_tok = _tokens(r.get("title", ""))
        snip_tok = _tokens(r.get("snippet", ""))
        if qtokens:
            title_cov = len(qtokens & title_tok) / len(qtokens)
            snip_cov = len(qtokens & snip_tok) / len(qtokens)
        else:
            title_cov = snip_cov = 0.0
        score = _TITLE_WEIGHT * title_cov + snip_cov
        matched = qtokens & (title_tok | snip_tok)
        has_overlap = bool(matched)
        is_fresh = bool(r.get("published"))
        # Recency boost is additive but applied ONLY to relevant (overlapping) results,
        # so an irrelevant fresh page (dropped below) never benefits - relevance wins.
        if recency and is_fresh and has_overlap:
            score += _RECENCY_BOOST
        # Generic-token down-weight: if the result matches at least one query token but
        # EVERY matched token is generic/low-information, shrink the score so a lone
        # generic overlap can't inflate an off-topic result.  A result matching any
        # content-bearing query token is left untouched (matched - _GENERIC_TOKENS
        # non-empty), preserving its exact score and ranking.
        if matched and matched <= _GENERIC_TOKENS:
            score *= _GENERIC_WEIGHT
        # Docker Hub host-penalty: if no docker intent and result is from hub.docker.com,
        # apply a multiplicative penalty so generic-token overlap scores sink below
        # _REL_FLOOR relative to strong results.  Does not affect has_overlap (the result
        # still counts as overlapping; we only shrink its magnitude for the floor gate).
        if _no_docker_intent and _host_matches(_host(r.get("url", "")), _DOCKER_HUB_DOMAIN):
            score *= _DOCKER_PENALTY
        fresh_rank = 0 if is_fresh else 1
        scored.append((score, fresh_rank, has_overlap, idx, r))

    # No usable query tokens (e.g. all <2 chars) -> nothing to rank against; return
    # the deduped results untouched in original order.
    if not qtokens:
        return [s[4] for s in sorted(scored, key=lambda s: s[3])]

    # Decide whether the hybrid semantic blend is active. None = auto (on iff the local
    # model stack is importable); True/False force it. similarities() failing falls back
    # to lexical, so semantics can never break search.
    use_semantic = semantic.available() if semantic_rerank is None else semantic_rerank
    sims: list[float] | None = None
    if use_semantic:
        try:
            sims = semantic.similarities(
                query,
                [f"{s[4].get('title', '')} {s[4].get('snippet', '')}" for s in scored],
            )
        except Exception:  # noqa: BLE001 - any semantic failure -> lexical fallback
            sims = None

    if sims is not None:
        return _rerank_hybrid(scored, sims)

    # Sort by descending score; then recency tiebreak (published first); then original
    # index. Score dominates, so relevance always wins - recency only breaks exact ties.
    def _key(s: tuple) -> tuple:
        return (-s[0], s[1], s[3])

    ranked = sorted(scored, key=_key)

    kept = [s for s in ranked if s[2]]
    if len(kept) < _MIN_KEEP:
        # Safety floor: BACKFILL with the backend's original top deduped results so we
        # never drop below the floor - without ever discarding the relevant results we
        # already kept (replacing the set with first-N-by-index threw away a relevant
        # tail hit in favor of pure junk).
        have = {s[3] for s in kept}
        backfill = [s for s in sorted(scored, key=lambda s: s[3]) if s[3] not in have]
        kept = sorted(kept + backfill[: _MIN_KEEP - len(kept)], key=_key)

    # Relative relevance gate: trim backfill whose score is far below the best result.
    kept = _rel_floor(kept, key=lambda s: s[0])
    return [s[4] for s in kept]


def _rerank_hybrid(
    scored: list[tuple[float, int, bool, int, dict]],
    sims: list[float],
) -> list[dict]:
    """Blend lexical + semantic scores, drop only clearly-irrelevant results, sort.

    ``scored`` rows are ``(lex, fresh_rank, has_overlap, idx, result)`` aligned to ``sims``.
    Blended = ``_SEM_WEIGHT*sem + (1-_SEM_WEIGHT)*lex_norm`` where ``lex_norm = lex/max_lex``
    (div0-guarded). Keep rule: drop a row only if it has NO lexical overlap AND ``sem``
    < ``_SEM_FLOOR`` (otherwise a paraphrase is rescued). Dedup already happened in
    ``scored``; ``_MIN_KEEP`` safety floor + recency tiebreak preserved.
    """
    max_lex = max((s[0] for s in scored), default=0.0)

    # rows: (blended, fresh_rank, idx, keep, result)
    rows: list[tuple[float, int, int, bool, dict]] = []
    for s, sem in zip(scored, sims, strict=True):
        lex, fresh_rank, has_overlap, idx, r = s
        lex_norm = lex / max_lex if max_lex > 0 else 0.0
        blended = _SEM_WEIGHT * sem + (1 - _SEM_WEIGHT) * lex_norm
        sem_ok = sem >= _SEM_FLOOR
        keep = has_overlap or sem_ok
        # Transient flag: lets search()'s lexical relevance guard credit a semantically
        # rescued (zero-lexical-overlap) row as relevant. Stripped before search() returns.
        r["_sem_relevant"] = sem_ok
        r["_sem_score"] = sem
        rows.append((blended, fresh_rank, idx, keep, r))

    # Blended score desc; recency tiebreak (published first); then original index (stable).
    def _key(row: tuple) -> tuple:
        return (-row[0], row[1], row[2])

    ranked = sorted(rows, key=_key)
    kept = [row for row in ranked if row[3]]
    if len(kept) < _MIN_KEEP:
        # Safety floor: BACKFILL around the kept (relevant) rows, never replace them.
        have = {row[2] for row in kept}
        backfill = [row for row in sorted(rows, key=lambda row: row[2]) if row[2] not in have]
        kept = sorted(kept + backfill[: _MIN_KEEP - len(kept)], key=_key)

    # Relative relevance gate: trim backfill whose blended score is far below the best.
    kept = _rel_floor(kept, key=lambda row: row[0])
    return [row[4] for row in kept]


async def _search_once(
    q: str,
    count: int,
    params: dict,
    base_url: str,
    client: "httpx.AsyncClient",
) -> tuple[list[dict], list]:
    """One full paginated pass. Returns (mapped_results, unresponsive_engines).

    Raises ``SearchError('search_backend_down')`` only on transport/HTTP/JSON errors.
    An empty list with a non-empty ``unresponsive`` signals a transient throttle the
    caller may retry; an empty list with empty ``unresponsive`` is a genuine miss.
    """
    seen: set[str] = set()
    results: list[dict] = []
    unresponsive: list = []
    for pageno in range(1, _MAX_PAGES + 1):
        try:
            resp = await client.get(
                f"{base_url}/search", params={**params, "pageno": pageno}
            )
            resp.raise_for_status()
            data = resp.json()
            page = data.get("results", [])
            unresponsive = data.get("unresponsive_engines", []) or unresponsive
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(
                "search_backend_down", f"SearXNG request failed: {exc}"
            ) from exc

        added = 0
        for raw in page:
            url = raw.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(_map_result(raw))
            added += 1

        if len(results) >= count or added == 0:
            break

    return results, unresponsive


def _fallback_base_urls(explicit: list[str] | None) -> list[str]:
    """Secondary SearXNG instances for SPOF failover: explicit arg wins, else the
    comma-separated ``ARGUS_SEARXNG_FALLBACKS`` env (empty by default -> no failover)."""
    if explicit is not None:
        return explicit
    raw = os.getenv("ARGUS_SEARXNG_FALLBACKS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _default_lang() -> str | None:
    """Default SearXNG language for stable relevance; empty env disables it."""
    raw = os.getenv(_DEFAULT_LANG_ENV)
    if raw is None:
        return _DEFAULT_LANG
    raw = raw.strip()
    return raw or None


def _is_low_relevance(query: str, results: list[dict]) -> bool:
    """True when most returned rows do not match content-bearing query tokens."""
    qtok = _tokens(query) - _STOPWORDS
    if not qtok or not results:
        return False
    guard_tokens = qtok - _GENERIC_TOKENS or qtok
    min_match = 2 if len(guard_tokens) >= 4 else 1
    overlap = sum(
        1
        for r in results
        for matched in [
            (_tokens(r.get("title", "")) | _tokens(r.get("snippet", ""))) & guard_tokens
        ]
        if (r.get("_sem_relevant") and r.get("_sem_score", 0.0) >= _SEM_GUARD_FLOOR)
        or len(matched) >= min_match
    )
    return overlap * 2 < len(results)


async def _search_backend(
    q: str,
    count: int,
    params: dict,
    base_url: str,
    client: "httpx.AsyncClient",
    retries: int,
) -> list[dict]:
    """Run the paginated search (with retry-on-throttle) against ONE SearXNG instance.

    Returns mapped results, or raises ``SearchError``: ``no_results`` when the backend
    answered but had nothing (never retried), ``search_backend_down`` on transport
    failure or when every engine stayed unresponsive across ``retries``.
    """
    results: list[dict] = []
    unresponsive: list = []
    for attempt in range(retries + 1):
        results, unresponsive = await _search_once(q, count, params, base_url, client)
        if results or not unresponsive:
            break  # got results, or a genuine no_results (don't retry)
        if attempt < retries:
            await asyncio.sleep(_BACKOFF_BASE * 2**attempt)
    if results:
        return results
    # Distinguish a transient backend throttle (engines suspended / timing out) from a
    # genuinely empty result, so callers don't treat rate-limiting as "nothing exists".
    if unresponsive:
        raise SearchError(
            "search_backend_down",
            f"all SearXNG engines unresponsive (rate-limited?): {unresponsive}",
        )
    raise SearchError("no_results", f"no results for query: {q!r}")


async def search(
    query: str | list[str],
    count: int = 10,
    category: str = "general",
    time_range: str | None = None,
    lang: str | None = None,
    base_url: str = "http://127.0.0.1:8888",
    client: "httpx.AsyncClient | None" = None,
    retries: int = 2,
    engines: list[str] | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    safesearch: int = 0,
    fallback_base_urls: list[str] | None = None,
    fallback_client: "httpx.AsyncClient | None" = None,
) -> dict:
    """Query a self-hosted SearXNG instance and return deduped, mapped results.

    Engine selection: an explicit ``engines`` list always wins (comma-joined into the
    ``engines`` param). Otherwise, a ``general`` query fans out across
    ``_DEFAULT_ENGINES`` to spread load so no single engine (DuckDuckGo) is the sole
    source. Non-``general`` categories keep using ``categories`` with no forced engines.

    Resilience: when an attempt yields ZERO results AND SearXNG reports
    ``unresponsive_engines`` (a transient throttle), the whole search is retried up to
    ``retries`` times with exponential backoff (``_BACKOFF_BASE * 2**attempt``) before
    raising ``search_backend_down``. A genuinely empty result (no unresponsive engines)
    raises ``no_results`` immediately and is never retried.

    Domain filters: ``include_domains`` keeps only results whose host suffix-matches one
    of the given domains (``example.com`` matches ``www.example.com``); ``exclude_domains``
    drops matching hosts. Both are POST-FILTERS applied to the mapped results BEFORE
    rerank/``[:count]``. If ``include_domains`` filtering leaves zero results, that is a
    legitimate ``no_results`` (distinct from a transient throttle).

    safesearch: passed through to SearXNG (``0`` off, ``1`` moderate, ``2`` strict);
    omitted from the request when left at the default ``0``.

    Recency: ``rerank`` is called with ``recency=True`` for recency-intent queries -
    ``category == 'news'`` OR ``time_range`` set - giving ``published``-bearing results a
    bounded additive boost. General queries keep ``recency=False`` (boost off; published
    remains only a tiebreak).
    """
    q = " ".join(query) if isinstance(query, list) else query
    categories = category if category in _VALID_CATEGORIES else "general"

    params = {"q": q, "format": "json", "categories": categories}
    if engines is not None:
        params["engines"] = ",".join(engines)
    elif categories == "general":
        params["engines"] = ",".join(_DEFAULT_ENGINES)
    if time_range:
        params["time_range"] = time_range
    effective_lang = lang if lang is not None else _default_lang()
    if effective_lang:
        params["language"] = effective_lang
    if safesearch:
        params["safesearch"] = str(safesearch)

    owns_client = client is None
    if owns_client:
        # Fast-fail a dead/hung SearXNG: a short connect timeout detects an unreachable
        # backend in ~_CONNECT_TIMEOUT s instead of hanging the full read timeout.
        client = httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT))

    backend = base_url
    degraded = False
    degraded_reason: str | None = None
    rescued_category: str | None = None
    fb_client = fallback_client
    fb_owns = False
    try:
        try:
            results = await _search_backend(q, count, params, base_url, client, retries)
        except SearchError as primary_exc:
            # A genuine empty result means the backend WORKED - never fail over for that.
            if primary_exc.code == "no_results":
                raise
            fallbacks = _fallback_base_urls(fallback_base_urls)
            if not fallbacks:
                raise
            # SPOF failover: the primary loopback SearXNG is down. Try owner-configured
            # secondary instances. These are EXTERNAL endpoints, so they go through the
            # SSRF-guarded client + validate_url (a fallback misconfigured to a private/
            # metadata IP is blocked). The loopback primary keeps its plain,
            # destination-fixed internal client - the trust boundary is unchanged.
            if fb_client is None:
                fb_client = build_safe_async_client(
                    timeout=httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT)
                )
                fb_owns = True
            results = None
            for fb in fallbacks:
                try:
                    validate_url(fb)
                    results = await _search_backend(q, count, params, fb, fb_client, retries)
                except (SearchError, SSRFError):
                    results = None
                    continue
                backend, degraded, degraded_reason = fb, True, "backend_failover"
                break
            if results is None:
                raise primary_exc
    finally:
        if owns_client:
            await client.aclose()
        if fb_owns and fb_client is not None:
            await fb_client.aclose()

    # Post-filter by domain BEFORE rerank/truncation. An include filter that removes
    # everything is a legitimate no_results (the backend had hits; none on the allowlist).
    if include_domains or exclude_domains:
        results = _filter_domains(results, include_domains, exclude_domains)
        if not results:
            raise SearchError(
                "no_results",
                f"no results for query {q!r} within domain filters",
            )

    # Recency intent: news category or an explicit time_range -> boost fresh results.
    recency = category == "news" or bool(time_range)

    # Deterministic relevance rerank + dedup decides what makes the top-`count`.
    # ARGUS_SEMANTIC_RERANK: 'auto' (default/unset) -> hybrid iff the local embedding stack is
    # available (A/B-validated +14.3% nDCG@5, +27.3% on conceptual queries); 'on'/'off' force it.
    # Ops kill-switch + in-prod A/B lever; None ('auto') preserves the existing auto behavior.
    _sr = {"on": True, "off": False}.get(os.getenv("ARGUS_SEMANTIC_RERANK", "auto").strip().lower())
    results = rerank(q, results, recency=recency, semantic_rerank=_sr)[:count]

    # Relevance guard: rerank keeps >= _MIN_KEEP results even when the backend returned ONLY
    # off-topic pages (SearXNG engine-suspension: all quality engines CAPTCHA on a datacenter IP
    # -> the sole surviving engine, throttled, returns generic filler that SearXNG parses as
    # results - e.g. AOL/grammar pages for a Hermes query). If FEWER THAN HALF the returned
    # results share a query token (title or snippet) with the query, the response is
    # untrustworthy: flag it degraded + reason so callers (research / the agent) never silently
    # treat junk as good results. Deterministic; majority-relevant sets are untouched.
    # Flag when MOST results are off-topic. A single incidental/generic token match must
    # not mask a garbage set: a throttled sole-engine can return filler that happens to
    # share one word with the query. Majority rule catches that while leaving on-topic sets.
    low_relevance = _is_low_relevance(q, results)
    if low_relevance:
        degraded = True
        if degraded_reason is None:
            degraded_reason = "low_relevance"

    # If the broad general pool is garbage, try one deterministic category rescue. This
    # only applies when the caller did not explicitly choose engines/domains; manual
    # constraints are respected exactly.
    if (
        low_relevance
        and categories == "general"
        and engines is None
        and not include_domains
        and not exclude_domains
    ):
        routed = classify(q)["route"]
        if routed in {"science", "it", "news"}:
            rescue_params = dict(params)
            rescue_params["categories"] = routed
            rescue_params.pop("engines", None)
            rescue_client = None
            try:
                if owns_client:
                    if backend == base_url:
                        rescue_client = httpx.AsyncClient(
                            timeout=httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT)
                        )
                    else:
                        validate_url(backend)
                        rescue_client = build_safe_async_client(
                            timeout=httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT)
                        )
                    rescue_results = await _search_backend(
                        q, count, rescue_params, backend, rescue_client, retries
                    )
                else:
                    rescue_results = await _search_backend(
                        q, count, rescue_params, backend, client, retries
                    )
                rescue_ranked = rerank(
                    q, rescue_results, recency=routed == "news" or bool(time_range),
                    semantic_rerank=_sr,
                )[:count]
                if not _is_low_relevance(q, rescue_ranked):
                    results = rescue_ranked
                    if degraded_reason == "low_relevance":
                        degraded, degraded_reason = False, None
                    rescued_category = routed
            except SearchError:
                pass
            finally:
                if rescue_client is not None:
                    await rescue_client.aclose()

    for r in results:  # strip the transient hybrid-rerank flag from the payload
        r.pop("_sem_relevant", None)
        r.pop("_sem_score", None)

    engines_used = sorted({r["engine"] for r in results if r["engine"]})
    return {
        "query": query,
        "results": results,
        "count": len(results),
        "engines_used": engines_used,
        "backend": backend,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "rescued_category": rescued_category,
    }
