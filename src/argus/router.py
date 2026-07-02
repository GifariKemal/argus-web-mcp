"""Deterministic query->domain router for ``smart_search`` (pure, offline).

A ``smart_search`` MCP tool calls :func:`classify` to auto-pick the best search backend
for a query, then dispatches itself. This module only CLASSIFIES - it is a pure function
of the lowercased query string: no network, no randomness, no LLM. Routes map to dispatch:

    "github"  -> github_search(repositories)          (code/repo/library-impl intent)
    "scholar" -> scholar_search(...)                  (academic/paper/research intent)
    "news"    -> search(category="news", time_range)  (recency/current-events intent)
    "it"      -> search(category="it")                (programming how-to/error/API intent)
    "general" -> search(category="general")           (default / everything else)

Scoring is additive: every matching keyword/phrase/pattern adds its weight to its route.
The highest-scoring route wins; an exact tie at the top (or no signal) is broken by
``_PRIORITY``: scholar > github > news > it > general. ``general`` is the zero baseline,
so it only wins when nothing else scored. Extend by adding entries to ``_TOKENS`` /
``_PHRASES`` / ``_PATTERNS`` (and a new route to ``ROUTES`` + ``_PRIORITY``).
"""

import re

ROUTES = ("github", "scholar", "news", "it", "general")

# Tiebreak order on EXACTLY equal top scores - higher-precision intents first.
_PRIORITY = ("scholar", "github", "news", "it", "general")

# Single-word signals: route -> {token: weight}. Matched against tokenized words.
_TOKENS: dict[str, dict[str, int]] = {
    "github": {
        "github": 3,
        "repo": 3,
        "repository": 3,
        "repositories": 3,
        "sdk": 2,
        "clone": 2,
        "npm": 2,
        "pip": 2,
    },
    "scholar": {
        "paper": 3,
        "papers": 3,
        "arxiv": 3,
        "doi": 3,
        "preprint": 3,
        "preprints": 3,
        "proceedings": 3,
        "study": 2,
        "studies": 2,
        "research": 2,
        "journal": 2,
        "citation": 2,
        "citations": 2,
        "dataset": 2,
        "datasets": 2,
    },
    "news": {
        "news": 3,
        "breaking": 3,
        "latest": 2,
        "today": 2,
        "recent": 2,
        "recently": 2,
        "announced": 2,
        "announcement": 2,
        "stock": 2,
        "stocks": 2,
    },
    "it": {
        "error": 2,
        "exception": 2,
        "function": 2,
        "api": 2,
        "install": 2,
        "config": 2,
        "configure": 2,
        "debug": 2,
        "syntax": 2,
        "compile": 2,
        "python": 2,
        "javascript": 2,
        "typescript": 2,
        "rust": 2,
        "golang": 2,
        "java": 2,
        "sql": 2,
        "bash": 2,
    },
}

# Multi-word phrase signals: route -> [(phrase, weight)]. Matched as substrings of the
# normalized (single-spaced) query. Phrases earn more than lone tokens.
_PHRASES: dict[str, list[tuple[str, int]]] = {
    "github": [
        ("source code", 4),
        ("open source", 3),
        ("library for", 3),
        ("npm package", 4),
        ("pip package", 4),
        ("on github", 4),
        ("implementation of", 3),
    ],
    "scholar": [
        ("peer-reviewed", 4),
        ("peer reviewed", 4),
        ("literature review", 4),
        ("state of the art", 3),
        ("survey of", 3),
        ("et al", 3),
    ],
    "news": [
        ("price today", 4),
        ("just released", 4),
        ("this week", 3),
        ("this year", 2),
    ],
    "it": [
        ("how to", 3),
        ("stack trace", 4),
        ("c++", 2),
        ("how do i", 3),
    ],
}

# Regex signals: route -> [(compiled_pattern, weight)]. For things tokens/phrases miss.
_PATTERNS: dict[str, list[tuple[re.Pattern[str], int]]] = {
    # 4-digit year >= 2025 -> recency.
    "news": [
        (re.compile(r"\b(20(2[5-9]|[3-9]\d))\b"), 3),
        # 'may' is excluded from the month alternation: as a modal verb ("what may
        # cause X") it misrouted ordinary questions to news. It only counts as a
        # month when date-anchored (a digit/year/temporal qualifier next to it).
        (
            re.compile(
                r"\b(january|february|march|april|june|july|august|"
                r"september|october|november|december)\b"
            ),
            2,
        ),
        (
            re.compile(
                r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+may|may\s+\d{1,2}|"
                r"may\s+20\d{2}|(?:in|early|late)\s+may)\b"
            ),
            2,
        ),
    ],
}

_WORD_RE = re.compile(r"[a-z0-9+#]+")  # keep '+'/'#' so 'c++'/'c#' survive tokenization


def _tokenize(text: str) -> list[str]:
    """Lowercase then split into alnum (+ ``+``/``#``) tokens."""
    return _WORD_RE.findall(text.lower())


def classify(query: str) -> dict:
    """Deterministic heuristic classifier.

    Tokenize the lowercased query, score each route by summed keyword/phrase/pattern
    weights, and pick the highest score. Exact top-score ties (and the no-signal case)
    resolve via ``_PRIORITY`` (scholar > github > news > it > general). Pure + fast.

    Returns ``{"route": str, "reason": str, "scores": {route: int}}``.
    """
    norm = " ".join(query.lower().split())  # collapse whitespace for phrase matching
    tokens = _tokenize(query)
    token_set = set(tokens)

    scores: dict[str, int] = {route: 0 for route in ROUTES}
    hits: dict[str, list[str]] = {route: [] for route in ROUTES}

    # 1) single-word tokens
    for route, table in _TOKENS.items():
        for token, weight in table.items():
            if token in token_set:
                scores[route] += weight
                hits[route].append(token)

    # 2) multi-word phrases (substring of normalized query)
    for route, phrases in _PHRASES.items():
        for phrase, weight in phrases:
            if phrase in norm:
                scores[route] += weight
                hits[route].append(phrase)

    # 3) regex patterns
    for route, patterns in _PATTERNS.items():
        for pattern, weight in patterns:
            match = pattern.search(norm)
            if match:
                scores[route] += weight
                hits[route].append(match.group(0))

    # 'general' stays the zero baseline - it wins only when no other route scored.
    top = max(scores[r] for r in ROUTES if r != "general")
    if top == 0:
        return {
            "route": "general",
            "reason": "no domain signal detected; defaulting to general search",
            "scores": scores,
        }

    # Highest score wins; exact ties broken by _PRIORITY order.
    winner = min(
        (r for r in ROUTES if r != "general" and scores[r] == top),
        key=_PRIORITY.index,
    )
    matched = ", ".join(dict.fromkeys(hits[winner]))  # dedupe, keep order
    return {
        "route": winner,
        "reason": f"matched {winner} signals: {matched} (score {top})",
        "scores": scores,
    }
