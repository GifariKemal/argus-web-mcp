"""Shared models + the structured-error helper.

Every MCP tool returns data or a structured error dict - it NEVER raises to the client.
"""

from pydantic import BaseModel

# Stable error codes used across tools (see docs/03-TOOL-SPECS.md).
ERROR_CODES = frozenset(
    {
        "ssrf_blocked",
        "fetch_failed",
        "empty_content",
        "not_pdf",
        "parse_failed",
        "search_backend_down",
        "no_results",
        "schema_invalid",
        "extraction_failed",
        "render_failed",
        "blocked_by_antibot",
        # Trading-tool structured codes, surfaced verbatim (masking them as generic
        # fetch_failed hid actionable causes like a bad report_type or date range).
        "cot_bad_report_type",
        "cot_fetch_failed",
        "ff_bad_date_range",
        "ff_bad_feed",
        "ff_fetch_failed",
    }
)


class ToolError(BaseModel):
    error: str
    code: str
    detail: str | None = None


# Per-code counts of structured tool errors since process start, exported as
# argus_tool_errors_total by /metrics. err() is only called at the server tool boundary,
# and the deploy is single-process/single-loop, so a plain dict is safe (same pattern as
# server._TOOL_CALLS). Without this, an SSRF block or a dead SearXNG is indistinguishable
# from success in Prometheus - errors are returned as dicts, never raised.
ERR_COUNTS: dict[str, int] = {}


def err(code: str, message: str, detail: str | None = None) -> dict:
    """Structured error dict for an MCP tool. Use instead of raising to the client."""
    if code not in ERROR_CODES:  # explicit (not assert - survives `python -O`)
        raise ValueError(f"unknown error code: {code}")
    ERR_COUNTS[code] = ERR_COUNTS.get(code, 0) + 1
    return ToolError(error=message, code=code, detail=detail).model_dump()
