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
        "rate_limited",
    }
)


class ToolError(BaseModel):
    error: str
    code: str
    detail: str | None = None


def err(code: str, message: str, detail: str | None = None) -> dict:
    """Structured error dict for an MCP tool. Use instead of raising to the client."""
    if code not in ERROR_CODES:  # explicit (not assert - survives `python -O`)
        raise ValueError(f"unknown error code: {code}")
    return ToolError(error=message, code=code, detail=detail).model_dump()
