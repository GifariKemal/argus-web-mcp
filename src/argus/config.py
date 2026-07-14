"""Argus server configuration — timeouts, metrics/health knobs.

All values load from environment variables with sensible production defaults.
No YAML file needed; 12-factor app style.
"""

from __future__ import annotations

import os


def _int(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, str(default)))
    except (ValueError, TypeError):
        return default


# Per-tool adaptive timeouts (seconds). Raised from hardcoded defaults based on QA data.
TIMEOUTS: dict[str, int] = {
    "read": _int("ARGUS_TIMEOUT_READ", 60),
    "search": _int("ARGUS_TIMEOUT_SEARCH", 60),
    "smart_search": _int("ARGUS_TIMEOUT_SMART_SEARCH", 120),
    "scrape": _int("ARGUS_TIMEOUT_SCRAPE", 90),
    "screenshot": _int("ARGUS_TIMEOUT_SCREENSHOT", 60),
    "read_pdf": _int("ARGUS_TIMEOUT_READ_PDF", 90),
    "research": _int("ARGUS_TIMEOUT_RESEARCH", 120),
    "crawl": _int("ARGUS_TIMEOUT_CRAWL", 180),
    "batch_read": _int("ARGUS_TIMEOUT_BATCH_READ", 120),
    "extract_structured": _int("ARGUS_TIMEOUT_EXTRACT", 90),
    "github_search": _int("ARGUS_TIMEOUT_GITHUB", 120),
    "scholar_search": _int("ARGUS_TIMEOUT_SCHOLAR", 120),
    "map_urls": _int("ARGUS_TIMEOUT_MAP", 60),
    "find_similar": _int("ARGUS_TIMEOUT_SIMILAR", 90),
    "forexfactory_calendar": _int("ARGUS_TIMEOUT_FOREX", 60),
    "cot_report": _int("ARGUS_TIMEOUT_COT", 90),
    "news_sentiment_feed": _int("ARGUS_TIMEOUT_NEWS", 90),
    "watch": _int("ARGUS_TIMEOUT_WATCH", 30),
    "list_watches": _int("ARGUS_TIMEOUT_WATCH", 30),
    "unwatch": _int("ARGUS_TIMEOUT_WATCH", 30),
}

def clamp_timeout(name: str, requested):
    """Clamp a CLIENT-supplied per-tool ``timeout`` to the server ceiling.

    ``timeout`` is a tool parameter, so a caller can pass any value; without a ceiling
    an inflated ``timeout=900`` lets a single scrape/research call run far past the
    intended bound (observed p99 tails). A client may ask for LESS but never MORE than
    ``TIMEOUTS[name]``. Non-numeric values pass through untouched (the tool validates).
    """
    ceiling = TIMEOUTS.get(name)
    if ceiling is None or isinstance(requested, bool) or not isinstance(requested, (int, float)):
        return requested
    return max(1, min(int(requested), ceiling))


# DNS resolution guard (seconds). The SSRF resolver runs off the event loop; this
# bounds it so a slow/hung resolver can't stall concurrent tool calls on the single worker.
DNS_TIMEOUT = _int("ARGUS_DNS_TIMEOUT", 5)

# Metrics / health
HEALTH_LATENCY_BUCKETS = _int("ARGUS_HEALTH_LATENCY_BUCKETS", 500)  # max latencies per tool
