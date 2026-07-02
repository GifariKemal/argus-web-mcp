"""CFTC Commitments of Traders - legacy futures-only report.

Source: ``https://www.cftc.gov/dea/newcot/deafut.txt`` (this-week legacy
futures-only, headerless CSV). Column order is the published CFTC legacy layout;
the positions satisfy the COT accounting identities, which the golden test pins:

    total_reportable_long  = noncommercial_long + noncommercial_spreads + commercial_long
    open_interest          = total_reportable_long + nonreportable_long

`parse_cot` is pure/deterministic; `cot_report` fetches via the SSRF-safe client.
"""

from __future__ import annotations

import csv
import io
from datetime import date as _date

from argus.security.ssrf import build_safe_async_client

REPORT_URLS = {
    "legacy_futures": "https://www.cftc.gov/dea/newcot/deafut.txt",
}
_TIMEOUT = 30.0

# Zero-based column indices in the headerless legacy futures-only CSV.
_COL = {
    "market": 0,
    "report_date": 2,  # YYYY-MM-DD
    "open_interest": 7,
    "noncommercial_long": 8,
    "noncommercial_short": 9,
    "noncommercial_spreads": 10,
    "commercial_long": 11,
    "commercial_short": 12,
    "nonreportable_long": 15,
    "nonreportable_short": 16,
}
_INT_FIELDS = (
    "open_interest",
    "noncommercial_long",
    "noncommercial_short",
    "noncommercial_spreads",
    "commercial_long",
    "commercial_short",
    "nonreportable_long",
    "nonreportable_short",
)
_MAX_COL = max(_COL.values())


class CotError(Exception):
    """Structured CFTC COT fetch/parse failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _to_int(value: str) -> int | None:
    """Parse a numeric COT cell to int; blank/non-numeric -> None."""
    s = value.strip().replace(",", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_cot(text: str | bytes, report_type: str = "legacy_futures") -> list[dict]:
    """Parse CFTC legacy futures-only COT text (headerless CSV) to row dicts.

    Each row -> ``{market, report_date, open_interest, noncommercial_long,
    noncommercial_short, noncommercial_spreads, commercial_long, commercial_short,
    nonreportable_long, nonreportable_short}`` with numeric fields as ``int``.
    Deterministic - preserves file order; skips short/blank lines.
    """
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")

    rows: list[dict] = []
    for record in csv.reader(io.StringIO(text)):
        if len(record) <= _MAX_COL:
            continue
        cell = [c.strip() for c in record]
        row = {
            "market": cell[_COL["market"]],
            "report_date": cell[_COL["report_date"]],
        }
        for field in _INT_FIELDS:
            row[field] = _to_int(cell[_COL[field]])
        rows.append(row)
    return rows


def _drift_checks(rows: list[dict]) -> tuple[int, int]:
    """Live column-layout drift detectors over parsed rows (pure, additive).

    Returns ``(identity_failures, bad_dates)``: rows violating the composed COT
    accounting identity ``open_interest == noncommercial_long + noncommercial_spreads
    + commercial_long + nonreportable_long`` (only counted when all five fields parsed),
    and rows whose ``report_date`` is not an ISO date. A CFTC layout change shifts the
    numeric columns, so either count > 0 means the data must be treated as degraded -
    without this, drift is only caught by the OFFLINE golden test, never live.
    """
    identity_failures = 0
    bad_dates = 0
    fields = ("open_interest", "noncommercial_long", "noncommercial_spreads",
              "commercial_long", "nonreportable_long")
    for row in rows:
        vals = [row[f] for f in fields]
        if all(v is not None for v in vals) and vals[0] != sum(vals[1:]):
            identity_failures += 1
        try:
            _date.fromisoformat(row["report_date"])
        except (ValueError, TypeError):
            bad_dates += 1
    return identity_failures, bad_dates


async def cot_report(report_type: str = "legacy_futures", date=None, *, client=None) -> dict:
    """Fetch + parse a CFTC COT report. Returns ``{rows, count, report_type, source,
    identity_failures, bad_dates}`` (+ ``requested_date`` when ``date`` is passed).

    ``date`` (YYYY-MM-DD, or any string whose first 10 chars are the date) filters rows
    to that exact report date - the this-week feed carries a single date, so a
    non-matching date returns an HONEST empty set instead of silently serving the
    wrong week. ``identity_failures``/``bad_dates`` > 0 signal CFTC column-layout
    drift (treat the data as degraded). Raises :class:`CotError` on fetch failure or
    an unknown ``report_type``.
    """
    source = REPORT_URLS.get(report_type)
    if source is None:
        raise CotError("cot_bad_report_type", f"unknown report_type: {report_type!r}")

    owns_client = client is None
    if owns_client:
        client = build_safe_async_client(timeout=_TIMEOUT)
    try:
        try:
            resp = await client.get(source)
            resp.raise_for_status()
            body = resp.content
        except Exception as exc:  # noqa: BLE001 - normalize to structured error
            raise CotError("cot_fetch_failed", f"CFTC COT fetch failed: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    rows = parse_cot(body, report_type=report_type)
    identity_failures, bad_dates = _drift_checks(rows)
    out = {
        "rows": rows, "count": len(rows), "report_type": report_type, "source": source,
        "identity_failures": identity_failures, "bad_dates": bad_dates,
    }
    if date:
        want = str(date)[:10]
        out["rows"] = [r for r in rows if r["report_date"] == want]
        out["count"] = len(out["rows"])
        out["requested_date"] = want
    return out
