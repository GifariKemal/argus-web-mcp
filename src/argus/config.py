"""Argus server configuration — timeouts, rate limits, feature flags.

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


def _float(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, str(default)))
    except (ValueError, TypeError):
        return default


def _bool(env: str, default: bool) -> bool:
    v = os.environ.get(env, "")
    return v.lower() in {"1", "true", "yes", "on"} if v else default


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

# Rate limiting
RATE_LIMIT_ENABLED = _bool("ARGUS_RATE_LIMIT_ENABLED", True)
RATE_LIMIT_RPM = _int("ARGUS_RATE_LIMIT_RPM", 60)          # requests per minute per IP
RATE_LIMIT_BURST = _int("ARGUS_RATE_LIMIT_BURST", 10)      # concurrent burst
RATE_LIMIT_LOCAL_BYPASS = _bool("ARGUS_RATE_LIMIT_LOCAL_BYPASS", True)

# Metrics / health
HEALTH_LATENCY_BUCKETS = _int("ARGUS_HEALTH_LATENCY_BUCKETS", 500)  # max latencies per tool
