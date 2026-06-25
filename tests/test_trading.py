"""Trading extractor tests - the >=99% field-accuracy HARD GATE.

Golden values are hand-verified against the real fixtures saved under
``tests/fixtures/`` (FairEconomy FF weekly JSON + CFTC legacy futures-only COT).
Fetch paths are exercised with an injected httpx client over MockTransport - no
live network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from argus.trading import cot, forexfactory, news

FIXTURES = Path(__file__).parent / "fixtures"
FF_FIXTURE = FIXTURES / "ff_calendar_sample.json"
COT_FIXTURE = FIXTURES / "cot_sample.txt"


@pytest.fixture(autouse=True)
def _reset_ff_last_good():
    """Reset the module-global stale-fallback store before/after each test so the
    forexfactory tests are order-independent (a prior success must not leak stale
    data into a later failure test)."""
    forexfactory._last_good = None
    yield
    forexfactory._last_good = None


# --------------------------------------------------------------------------- #
# ForexFactory golden                                                         #
# --------------------------------------------------------------------------- #

# Hand-verified from ff_calendar_sample.json (12 real FairEconomy events,
# fetched 2026-06-24 from nfs.faireconomy.media/ff_calendar_thisweek.json).
# Empty forecast/previous -> None; no `actual` present this week -> None.
FF_GOLDEN = [
    {"time": "2026-06-21T21:00:00-04:00", "currency": "CNY", "event": "1-y Loan Prime Rate",
     "impact": "Low", "actual": None, "forecast": "3.00%", "previous": "3.00%"},
    {"time": "2026-06-21T21:00:00-04:00", "currency": "CNY", "event": "5-y Loan Prime Rate",
     "impact": "Low", "actual": None, "forecast": "3.50%", "previous": "3.50%"},
    {"time": "2026-06-21T23:00:00-04:00", "currency": "NZD", "event": "Credit Card Spending y/y",
     "impact": "Low", "actual": None, "forecast": None, "previous": "2.9%"},
    {"time": "2026-06-22T05:06:00-04:00", "currency": "CNY",
     "event": "Foreign Direct Investment ytd/y",
     "impact": "Low", "actual": None, "forecast": None, "previous": "-10.3%"},
    {"time": "2026-06-22T07:00:00-04:00", "currency": "EUR",
     "event": "German Buba President Nagel Speaks",
     "impact": "Low", "actual": None, "forecast": None, "previous": None},
    {"time": "2026-06-22T08:30:00-04:00", "currency": "CAD", "event": "CPI m/m",
     "impact": "High", "actual": None, "forecast": "0.7%", "previous": "0.4%"},
    {"time": "2026-06-22T08:30:00-04:00", "currency": "CAD", "event": "Median CPI y/y",
     "impact": "High", "actual": None, "forecast": "2.1%", "previous": "2.1%"},
    {"time": "2026-06-22T08:30:00-04:00", "currency": "CAD", "event": "Trimmed CPI y/y",
     "impact": "High", "actual": None, "forecast": "2.0%", "previous": "2.0%"},
    {"time": "2026-06-22T08:30:00-04:00", "currency": "CAD", "event": "Common CPI y/y",
     "impact": "Medium", "actual": None, "forecast": "2.5%", "previous": "2.5%"},
    {"time": "2026-06-22T08:30:00-04:00", "currency": "CAD", "event": "Core CPI m/m",
     "impact": "Low", "actual": None, "forecast": None, "previous": "0.2%"},
    {"time": "2026-06-22T09:00:00-04:00", "currency": "EUR",
     "event": "ECB President Lagarde Speaks",
     "impact": "Medium", "actual": None, "forecast": None, "previous": None},
    {"time": "2026-06-22T09:00:00-04:00", "currency": "USD", "event": "FOMC Member Waller Speaks",
     "impact": "Low", "actual": None, "forecast": None, "previous": None},
]


def test_parse_ff_calendar_golden():
    feed = json.loads(FF_FIXTURE.read_text(encoding="utf-8"))
    parsed = forexfactory.parse_ff_calendar(feed)

    assert len(parsed) == len(FF_GOLDEN) == 12
    # EXACT field-by-field equality on all 12 events => 100% field accuracy.
    for got, want in zip(parsed, FF_GOLDEN, strict=True):
        assert got == want, f"event mismatch: {got!r} != {want!r}"


def test_parse_ff_calendar_accepts_json_string():
    parsed = forexfactory.parse_ff_calendar(FF_FIXTURE.read_text(encoding="utf-8"))
    assert parsed == FF_GOLDEN


def test_parse_ff_calendar_impact_normalization_and_missing_actual():
    feed = [
        {"title": "A", "country": "USD", "date": "2026-01-01T00:00:00-05:00", "impact": "HIGH",
         "forecast": "1", "previous": "2", "actual": "3"},
        {"title": "B", "country": "EUR", "date": "2026-01-01T00:00:00-05:00", "impact": "medium"},
        {"title": "C", "country": "GBP", "date": "2026-01-01T00:00:00-05:00", "impact": "Holiday"},
        {"title": "D", "country": "JPY", "date": "2026-01-01T00:00:00-05:00", "impact": ""},
        {"title": "E", "country": "AUD", "date": "2026-01-01T00:00:00-05:00", "impact": "Low",
         "forecast": "", "previous": "  ", "actual": ""},
    ]
    parsed = forexfactory.parse_ff_calendar(feed)
    assert [e["impact"] for e in parsed] == ["High", "Medium", "Holiday", "Holiday", "Low"]
    # actual present on A, missing/blank -> None elsewhere.
    assert parsed[0]["actual"] == "3"
    assert parsed[1]["actual"] is None  # key absent
    assert parsed[4]["actual"] is None  # empty string
    assert parsed[4]["forecast"] is None and parsed[4]["previous"] is None


def _mock_client(content: bytes, status: int = 200) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_forexfactory_calendar_shape_and_count():
    body = FF_FIXTURE.read_bytes()
    async with _mock_client(body) as client:
        out = await forexfactory.forexfactory_calendar(client=client)
    assert out["count"] == 12
    assert out["source"] == forexfactory.FEED_URL
    assert out["events"] == FF_GOLDEN


async def test_forexfactory_calendar_date_range_filter():
    body = FF_FIXTURE.read_bytes()
    async with _mock_client(body) as client:
        out = await forexfactory.forexfactory_calendar(
            date_range=("2026-06-22", "2026-06-22"), client=client
        )
    assert out["count"] == 9  # 3 events on 06-21 excluded
    assert all(e["time"].startswith("2026-06-22") for e in out["events"])


async def test_forexfactory_calendar_date_range_reversed_bounds_auto_swap():
    # lo > hi must be swapped so the filter still matches the intended window.
    body = FF_FIXTURE.read_bytes()
    async with _mock_client(body) as client:
        out = await forexfactory.forexfactory_calendar(
            date_range=("2026-06-22", "2026-06-21"), client=client  # reversed
        )
    # Same window as ("2026-06-21","2026-06-22") -> all 12 events.
    assert out["count"] == 12


async def test_forexfactory_calendar_excludes_empty_time_event():
    # An event with empty/missing date -> time is None -> excluded by _in_range.
    feed = [
        {"title": "Has date", "country": "USD", "date": "2026-06-22T08:30:00-04:00",
         "impact": "High"},
        {"title": "No date", "country": "EUR", "date": "", "impact": "Low"},
    ]
    body = json.dumps(feed).encode("utf-8")
    async with _mock_client(body) as client:
        out = await forexfactory.forexfactory_calendar(
            date_range=("2026-06-22", "2026-06-22"), client=client
        )
    assert out["count"] == 1
    assert out["events"][0]["event"] == "Has date"


async def test_forexfactory_calendar_fetch_failure_raises_coded():
    # No last-good to fall back on -> a hard fetch failure still raises.
    async with _mock_client(b"err", status=503) as client:
        with pytest.raises(forexfactory.ForexFactoryError) as ei:
            await forexfactory.forexfactory_calendar(client=client)
    assert ei.value.code == "ff_fetch_failed"


async def test_forexfactory_fresh_marks_stale_false():
    body = FF_FIXTURE.read_bytes()
    async with _mock_client(body) as client:
        out = await forexfactory.forexfactory_calendar(client=client)
    assert out["stale"] is False


async def test_forexfactory_serves_stale_flagged_on_failure():
    # A recent last-good IS served when the live fetch fails - but FLAGGED, never silent.
    forexfactory._last_good = {"events": FF_GOLDEN, "ts": time.time() - 120}
    async with _mock_client(b"err", status=503) as client:
        out = await forexfactory.forexfactory_calendar(client=client)
    assert out["stale"] is True
    assert out["count"] == 12
    assert out["events"] == FF_GOLDEN
    assert out["stale_age_seconds"] >= 0
    assert "fetched_at" in out


async def test_forexfactory_stale_too_old_raises():
    # Last-good older than the 6h window -> do NOT serve it; raise instead.
    forexfactory._last_good = {"events": FF_GOLDEN, "ts": time.time() - 7 * 3600}
    async with _mock_client(b"err", status=503) as client:
        with pytest.raises(forexfactory.ForexFactoryError) as ei:
            await forexfactory.forexfactory_calendar(client=client)
    assert ei.value.code == "ff_fetch_failed"


async def test_forexfactory_retries_then_succeeds():
    # First attempt fails (transient), retry succeeds -> fresh result, 2 attempts.
    body = FF_FIXTURE.read_bytes()
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, content=b"transient")
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await forexfactory.forexfactory_calendar(client=client)
    assert calls["n"] == 2
    assert out["stale"] is False
    assert out["count"] == 12


# --------------------------------------------------------------------------- #
# CFTC COT golden                                                             #
# --------------------------------------------------------------------------- #

# Hand-verified from cot_sample.txt (CFTC legacy futures-only deafut.txt,
# fetched 2026-06-24, report date 2026-06-16). Column mapping confirmed via the
# COT accounting identities (TR_long = nc_long + spreads + comm_long; OI =
# TR_long + nonreportable_long).
COT_GOLDEN = [
    {"market": "WHEAT-SRW - CHICAGO BOARD OF TRADE", "report_date": "2026-06-16",
     "open_interest": 444641, "noncommercial_long": 116522, "noncommercial_short": 167334,
     "noncommercial_spreads": 121144, "commercial_long": 171773, "commercial_short": 120524,
     "nonreportable_long": 35202, "nonreportable_short": 35639},
    {"market": "WHEAT-HRW - CHICAGO BOARD OF TRADE", "report_date": "2026-06-16",
     "open_interest": 289534, "noncommercial_long": 65912, "noncommercial_short": 75150,
     "noncommercial_spreads": 75085, "commercial_long": 128177, "commercial_short": 116082,
     "nonreportable_long": 20360, "nonreportable_short": 23217},
    {"market": "WHEAT-HRSpring - MIAX FUTURES EXCHANGE", "report_date": "2026-06-16",
     "open_interest": 83639, "noncommercial_long": 22740, "noncommercial_short": 15381,
     "noncommercial_spreads": 5699, "commercial_long": 46678, "commercial_short": 52253,
     "nonreportable_long": 8522, "nonreportable_short": 10306},
]


def test_parse_cot_golden():
    rows = cot.parse_cot(COT_FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 5  # fixture has 5 real rows
    for got, want in zip(rows[:3], COT_GOLDEN, strict=True):
        assert got == want, f"COT row mismatch: {got!r} != {want!r}"
    # every numeric field is an int (never str/float).
    for row in rows:
        for k, v in row.items():
            if k not in ("market", "report_date"):
                assert isinstance(v, int), f"{k} not int: {v!r}"


def test_parse_cot_accounting_identities_hold():
    rows = cot.parse_cot(COT_FIXTURE.read_bytes())
    for r in rows:
        tr_long = r["noncommercial_long"] + r["noncommercial_spreads"] + r["commercial_long"]
        assert r["open_interest"] == tr_long + r["nonreportable_long"]


def test_parse_cot_blank_numeric_to_none():
    text = "TEST MARKET,260616,2026-06-16,XXX,EX,00,001," + ",".join([""] * 122)
    rows = cot.parse_cot(text)
    assert rows[0]["market"] == "TEST MARKET"
    assert rows[0]["open_interest"] is None


def test_parse_cot_non_numeric_cell_to_none():
    # _MAX_COL is 16, so a row needs >16 columns. open_interest (col 7) is the
    # non-numeric token "N.A." -> _to_int returns None; other int cells parse.
    cells = ["MKT NONNUM", "260616", "2026-06-16", "x", "y", "z", "w"]
    cells.append("N.A.")  # col 7: open_interest, non-numeric
    cells += [str(i) for i in range(8, 20)]  # cols 8..19 numeric, > _MAX_COL
    rows = cot.parse_cot(",".join(cells))
    assert len(rows) == 1
    assert rows[0]["market"] == "MKT NONNUM"
    assert rows[0]["open_interest"] is None  # non-numeric -> None
    assert rows[0]["noncommercial_long"] == 8  # col 8 still parsed as int


def test_parse_cot_skips_too_short_line():
    short = "ONLY,A,FEW,COLUMNS"  # 4 cols <= _MAX_COL (16) -> skipped
    valid = ["GOOD MKT", "260616", "2026-06-16"] + [str(i) for i in range(3, 20)]
    text = short + "\n" + ",".join(valid)
    rows = cot.parse_cot(text)
    assert len(rows) == 1  # short line dropped
    assert rows[0]["market"] == "GOOD MKT"


async def test_cot_report_shape_and_count():
    body = COT_FIXTURE.read_bytes()
    async with _mock_client(body) as client:
        out = await cot.cot_report(client=client)
    assert out["count"] == 5
    assert out["report_type"] == "legacy_futures"
    assert out["source"] == cot.REPORT_URLS["legacy_futures"]
    assert out["rows"][0] == COT_GOLDEN[0]


async def test_cot_report_unknown_type_raises():
    with pytest.raises(cot.CotError) as ei:
        await cot.cot_report(report_type="bogus", client=_mock_client(b""))
    assert ei.value.code == "cot_bad_report_type"


async def test_cot_report_fetch_failure_raises_coded():
    async with _mock_client(b"err", status=500) as client:
        with pytest.raises(cot.CotError) as ei:
            await cot.cot_report(client=client)
    assert ei.value.code == "cot_fetch_failed"


# --------------------------------------------------------------------------- #
# News sentiment feed                                                         #
# --------------------------------------------------------------------------- #

CANNED_SEARCH = {
    "query": "gold",
    "results": [
        {"title": "Gold rallies", "url": "https://ex.com/1", "snippet": "Gold up on yields",
         "engine": "bing news", "published": "2026-06-23T10:00:00Z"},
        {"title": "Dollar firms", "url": "https://ex.com/2", "snippet": "USD higher",
         "engine": "bing news"},
    ],
    "count": 2,
    "engines_used": ["bing news"],
}


@pytest.fixture
def canned_search(monkeypatch):
    captured = {}

    async def fake_search(query, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return CANNED_SEARCH

    monkeypatch.setattr(news, "web_search", fake_search)
    return captured


async def test_news_feed_no_sentiment(canned_search):
    out = await news.news_sentiment_feed("gold")
    assert out["query"] == "gold"
    assert out["count"] == 2
    assert canned_search["kwargs"]["category"] == "news"
    assert out["items"][0]["published"] == "2026-06-23T10:00:00Z"
    assert "published" not in out["items"][1]
    assert all("score" not in it for it in out["items"])


async def test_news_feed_since_passed_as_time_range(canned_search):
    await news.news_sentiment_feed("gold", since="week")
    assert canned_search["kwargs"]["time_range"] == "week"


async def test_news_feed_sentiment_graceful_when_llm_unavailable(canned_search, monkeypatch):
    # LLM present but reports unavailable -> no scores, no failure.
    async def fake_extract(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("extract_llm should not run when llm_available() is False")

    monkeypatch.setattr(news, "_llm_hooks", lambda: (fake_extract, lambda: False))
    out = await news.news_sentiment_feed("gold", sentiment=True)
    assert all("score" not in it for it in out["items"])


async def test_news_feed_sentiment_graceful_when_llm_module_missing(canned_search, monkeypatch):
    # Import failure path -> _llm_hooks returns (None, None) -> no scores.
    monkeypatch.setattr(news, "_llm_hooks", lambda: (None, None))
    out = await news.news_sentiment_feed("gold", sentiment=True)
    assert all("score" not in it for it in out["items"])


async def test_news_feed_sentiment_present_when_llm_available(canned_search, monkeypatch):
    # Signature mirrors the REAL extract_llm(content, schema, prompt=None, client=None) so a
    # wrong kwarg (e.g. instruction=) would raise TypeError here instead of being swallowed.
    async def fake_extract(content, schema, prompt=None, client=None):
        assert prompt and "sentiment" in prompt.lower()  # the call must pass prompt=
        return {"score": 0.8 if "rallies" in content or "Gold up" in content else 5.0}

    monkeypatch.setattr(news, "_llm_hooks", lambda: (fake_extract, lambda: True))
    out = await news.news_sentiment_feed("gold", sentiment=True)
    assert out["items"][0]["score"] == 0.8
    assert out["items"][1]["score"] == 1.0  # 5.0 clamped to [-1, 1]
