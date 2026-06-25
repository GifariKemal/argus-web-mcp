"""ForexFactory economic calendar via the FairEconomy weekly JSON feed.

The public feed (`https://nfs.faireconomy.media/ff_calendar_thisweek.json`) is a
clean structured array - no Cloudflare scraping needed. Each element looks like::

    {"title": "CPI m/m", "country": "CAD", "date": "2026-06-22T08:30:00-04:00",
     "impact": "High", "forecast": "0.7%", "previous": "0.4%", "actual": ...}

`parse_ff_calendar` maps each event to the Aurix ``calendar_client`` shape and is
pure/deterministic; `forexfactory_calendar` fetches via the SSRF-safe client.

Aurix alignment (verified against Music/Aurix/fundamentals/calendar_client.py, 2026-06-24):
Aurix consumes the SAME FairEconomy feed and emits keys
``{date, name, currency, impact, actual, forecast, previous, gold_relevant}``.
Argus emits ``{time, event, currency, impact, actual, forecast, previous}`` - to feed Aurix
consumers directly, map ``time -> date`` and ``event -> name`` (and add ``gold_relevant`` via
Aurix's own keyword filter if needed). currency/impact/actual/forecast/previous are identical.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

from argus.security.ssrf import build_safe_async_client

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_TIMEOUT = 20.0
# Resilience (the FairEconomy feed is an external single point of failure): a tight
# per-attempt timeout + one retry, then a STALE-but-FLAGGED fallback to the last good
# fetch (<= _STALE_MAX_AGE). Stale data is never served silently - it carries
# `stale: True` + `stale_age_seconds` so a trading consumer can decide to trust it.
_ATTEMPT_TIMEOUT = 15.0
_MAX_ATTEMPTS = 2  # initial + 1 retry
_STALE_MAX_AGE = 6 * 3600  # serve last-good up to 6h on fetch failure
# Last successful FULL (unfiltered) fetch. Module-global = per-process (single worker).
# ponytail: in-module last-good store instead of touching the shared cache layer.
_last_good: dict | None = None

# FairEconomy impact strings -> normalized Aurix labels. Anything else (e.g. a
# bank holiday flagged on the feed) folds into "Holiday".
_IMPACT_MAP = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "holiday": "Holiday",
    "non-economic": "Holiday",
}


class ForexFactoryError(Exception):
    """Structured FairEconomy fetch failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _normalize_impact(raw: object) -> str:
    return _IMPACT_MAP.get(str(raw or "").strip().lower(), "Holiday")


def _clean(value: object) -> str | None:
    """Empty/whitespace/missing -> None; otherwise the stripped string."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_ff_calendar(feed: object) -> list[dict]:
    """Map a parsed FairEconomy feed (JSON list or JSON str) to Aurix events.

    Each event -> ``{time, currency, event, impact, actual, forecast, previous}``.
    Missing/empty ``actual``/``forecast``/``previous`` become ``None``.
    Deterministic - preserves feed order.
    """
    if isinstance(feed, (str, bytes, bytearray)):
        feed = json.loads(feed)
    if not isinstance(feed, list):
        raise ForexFactoryError("ff_bad_feed", "FairEconomy feed is not a JSON list")

    events: list[dict] = []
    for raw in feed:
        events.append(
            {
                "time": _clean(raw.get("date")),
                "currency": _clean(raw.get("country")),
                "event": _clean(raw.get("title")),
                "impact": _normalize_impact(raw.get("impact")),
                "actual": _clean(raw.get("actual")),
                "forecast": _clean(raw.get("forecast")),
                "previous": _clean(raw.get("previous")),
            }
        )
    return events


def _in_range(time: str | None, lo: str, hi: str) -> bool:
    """Date-prefix (YYYY-MM-DD) comparison; missing time -> excluded."""
    if not time:
        return False
    day = time[:10]
    return lo <= day <= hi


def _filter_range(events: list[dict], date_range) -> list[dict]:
    """Filter events to a (start, end) inclusive YYYY-MM-DD window (auto-swapped)."""
    if not date_range:
        return events
    lo, hi = date_range[0][:10], date_range[1][:10]
    if lo > hi:
        lo, hi = hi, lo
    return [e for e in events if _in_range(e["time"], lo, hi)]


async def forexfactory_calendar(date_range=None, *, client=None) -> dict:
    """Fetch + parse the FairEconomy FF feed, optionally filtered by date range.

    ``date_range`` is a (start, end) tuple/list of ISO dates (inclusive, compared
    on the YYYY-MM-DD prefix) or ``None``. Returns
    ``{events, count, source, stale}``.

    Resilience: a tight per-attempt timeout + one retry. If the external feed is
    still unreachable, falls back to the last good fetch (<= 6h old) flagged
    ``stale: True`` (+ ``stale_age_seconds``, ``fetched_at``) so a trading consumer
    can decide whether to trust it - stale data is NEVER served silently. Raises
    :class:`ForexFactoryError` only when there is no recent last-good to fall back on.
    """
    global _last_good
    owns_client = client is None
    if owns_client:
        client = build_safe_async_client(timeout=_TIMEOUT)
    body = None
    last_exc: Exception | None = None
    try:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(FEED_URL, timeout=_ATTEMPT_TIMEOUT)
                resp.raise_for_status()
                body = resp.content
                break
            except Exception as exc:  # noqa: BLE001 - retry, then stale-fallback
                last_exc = exc
                if attempt + 1 < _MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * (attempt + 1))
    finally:
        if owns_client:
            await client.aclose()

    if body is None:
        # Fetch failed after retries. Serve the last good feed if recent, FLAGGED stale.
        if _last_good is not None:
            age = time.time() - _last_good["ts"]
            if age <= _STALE_MAX_AGE:
                events = _filter_range(_last_good["events"], date_range)
                return {
                    "events": events,
                    "count": len(events),
                    "source": FEED_URL,
                    "stale": True,
                    "stale_age_seconds": int(age),
                    "fetched_at": datetime.fromtimestamp(
                        _last_good["ts"], tz=UTC
                    ).isoformat(),
                }
        raise ForexFactoryError(
            "ff_fetch_failed", f"FairEconomy feed fetch failed: {last_exc}"
        ) from last_exc

    all_events = parse_ff_calendar(body)
    _last_good = {"events": all_events, "ts": time.time()}
    events = _filter_range(all_events, date_range)
    return {"events": events, "count": len(events), "source": FEED_URL, "stale": False}
