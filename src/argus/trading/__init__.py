"""Trading-specialized extractors - the Argus moat (P2).

Clean structured feeds over Cloudflare scraping:
- ForexFactory economic calendar via the FairEconomy weekly JSON feed.
- CFTC Commitments of Traders (legacy futures-only) report.
- News ranking with optional owned-LLM sentiment scoring.

Golden-file tested at >=99% field accuracy before any live Aurix use.
"""

from argus.trading.cot import cot_report, parse_cot
from argus.trading.forexfactory import forexfactory_calendar, parse_ff_calendar
from argus.trading.news import news_sentiment_feed

__all__ = [
    "parse_ff_calendar",
    "forexfactory_calendar",
    "parse_cot",
    "cot_report",
    "news_sentiment_feed",
]
