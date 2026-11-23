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
# FairEconomy rate-limits per IP and the primary server's IP is shared with other feed
# consumers (429 + Retry-After 262 s, live 2026-11-23). A recent success is reused as
# fresh, a 429 is not retried, and upstream is left alone until Retry-After has passed.
_FRESH_TTL = 300
_RETRY_AFTER_DEFAULT = 300
_RETRY_AFTER_MAX = 3600
_blocked_until = 0.0
_locks: dict = {}


def _retry_after(value: str | None) -> int:
    try:
        return max(1, min(_RETRY_AFTER_MAX, int(value or "")))
    except ValueError:
        return _RETRY_AFTER_DEFAULT

# FairEconomy impact strings -> normalized Aurix labels. Unknown NON-EMPTY labels pass
# through verbatim so feed drift stays visible (folding them into "Holiday" would hide a
# potentially high-impact event class); empty/missing -> "Holiday" (feed marks true
# holiday rows with an empty impact).
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
    s = str(raw or "").strip()
    return _IMPACT_MAP.get(s.lower(), s or "Holiday")


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
        if not isinstance(raw, dict):  # junk element must not kill the whole calendar
            continue
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
    """Filter events to a (start, end) inclusive YYYY-MM-DD window (auto-swapped).

    Bounds are VALIDATED and normalized to ISO calendar dates - a malformed range
    ('2026-6-2', an int, a single element) raises ``ForexFactoryError('ff_bad_date_range')``
    instead of silently lexicographic-comparing garbage and returning the wrong event set.
    """
    if not date_range:
        return events
    if len(date_range) != 2:
        raise ForexFactoryError(
            "ff_bad_date_range", f"date_range must be [start, end], got {date_range!r}"
        )
    try:
        lo, hi = (datetime.fromisoformat(str(b)[:10]).date().isoformat() for b in date_range)
    except (ValueError, TypeError) as exc:
        raise ForexFactoryError(
            "ff_bad_date_range", f"invalid ISO date bound in {date_range!r}"
        ) from exc
    if lo > hi:
        lo, hi = hi, lo
    return [e for e in events if _in_range(e["time"], lo, hi)]


async def forexfactory_calendar(date_range=None, *, client=None) -> dict:
    """Fetch + parse the FairEconomy FF feed, optionally filtered by date range.

    ``date_range`` is a (start, end) tuple/list of ISO dates (inclusive, compared
    on the YYYY-MM-DD prefix) or ``None``. Returns
    ``{events, count, source, stale}``.

    A feed fetched less than _FRESH_TTL ago is reused (one upstream fetch at a time) and
    marked ``cached`` with ``age_seconds``/``fetched_at``; a 429 is honoured through its
    Retry-After (FairEconomy limits per IP, and the host IP is shared).

    Resilience: a tight per-attempt timeout + one retry. If the external feed is
    still unreachable, falls back to the last good fetch (<= 6h old) flagged
    ``stale: True`` (+ ``stale_age_seconds``, ``fetched_at``) so a trading consumer
    can decide whether to trust it - stale data is NEVER served silently. Raises
    :class:`ForexFactoryError` only when there is no recent last-good to fall back on.
    """
    global _last_good
    if (reply := _reuse(date_range)) is not None:
        return reply
    async with _fetch_lock():
        if (reply := _reuse(date_range)) is not None:  # a concurrent call just fetched
            return reply
        body, last_exc = await _fetch(client)
        fresh = None if body is None else parse_ff_calendar(body)
        if fresh is not None:
            _last_good = {"events": fresh, "ts": time.time()}

    if fresh is None:
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

    events = _filter_range(fresh, date_range)
    return {"events": events, "count": len(events), "source": FEED_URL, "stale": False}


def _reuse(date_range) -> dict | None:
    """The last good feed while younger than _FRESH_TTL, marked ``cached`` with its age:
    an ``actual`` printed since then is not in it, and the consumer can see that."""
    if _last_good is None:
        return None
    age = time.time() - _last_good["ts"]
    if age >= _FRESH_TTL:
        return None
    events = _filter_range(_last_good["events"], date_range)
    return {"events": events, "count": len(events), "source": FEED_URL, "stale": False,
            "cached": True, "age_seconds": int(age),
            "fetched_at": datetime.fromtimestamp(_last_good["ts"], tz=UTC).isoformat()}


def _fetch_lock() -> asyncio.Lock:
    """One upstream fetch at a time (per event loop: production runs one, tests many)."""
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks.clear()
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


async def _fetch(client) -> tuple[bytes | None, Exception | None]:
    """One upstream attempt plus one retry; a 429 is not retried and blocks upstream until
    its Retry-After has passed. Returns (body or None, last error)."""
    global _blocked_until
    now = time.time()
    if now < _blocked_until:
        return None, RuntimeError(
            f"rate limited by FairEconomy, retry in {int(_blocked_until - now) + 1}s")
    last_exc: Exception | None = None
    owns_client = client is None
    if owns_client:
        client = build_safe_async_client(timeout=_TIMEOUT)
    try:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(FEED_URL, timeout=_ATTEMPT_TIMEOUT)
                if resp.status_code == 429:
                    wait = _retry_after(resp.headers.get("retry-after"))
                    _blocked_until = time.time() + wait
                    return None, RuntimeError(f"HTTP 429 from FairEconomy, retry in {wait}s")
                resp.raise_for_status()
                return resp.content, None
            except Exception as exc:  # noqa: BLE001 - retry, then stale-fallback
                last_exc = exc
                if attempt + 1 < _MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * (attempt + 1))
    finally:
        if owns_client:
            await client.aclose()
    return None, last_exc
