"""Tests for argus.config — per-tool timeouts and env overrides."""

import importlib

from argus import config

# The 20 live MCP tools plus the two watch aliases all carry a configured timeout.
_EXPECTED_TIMEOUT_KEYS = {
    "read",
    "search",
    "smart_search",
    "scrape",
    "screenshot",
    "read_pdf",
    "research",
    "crawl",
    "batch_read",
    "extract_structured",
    "github_search",
    "scholar_search",
    "map_urls",
    "find_similar",
    "forexfactory_calendar",
    "cot_report",
    "news_sentiment_feed",
    "watch",
    "list_watches",
    "unwatch",
}


def test_timeouts_has_expected_keys_with_int_values():
    assert _EXPECTED_TIMEOUT_KEYS <= set(config.TIMEOUTS)
    for key in _EXPECTED_TIMEOUT_KEYS:
        val = config.TIMEOUTS[key]
        assert isinstance(val, int), f"{key} timeout is not an int: {val!r}"
        assert val > 0


def test_env_override_is_honored(monkeypatch):
    monkeypatch.setenv("ARGUS_TIMEOUT_READ", "7")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.TIMEOUTS["read"] == 7
    finally:
        # monkeypatch restores the env on teardown; reload to restore module state.
        monkeypatch.delenv("ARGUS_TIMEOUT_READ", raising=False)
        importlib.reload(config)
    # confirm the default is back after restore
    assert config.TIMEOUTS["read"] == 60


def test_int_falls_back_on_bad_value(monkeypatch):
    monkeypatch.setenv("ARGUS_TIMEOUT_READ", "not-a-number")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.TIMEOUTS["read"] == 60
    finally:
        monkeypatch.delenv("ARGUS_TIMEOUT_READ", raising=False)
        importlib.reload(config)


def test_tool_specs_doc_timeouts_match_config():
    """docs/03-TOOL-SPECS.md tool-signature timeout literals must match config.TIMEOUTS
    (guards the drift that had 5 of 6 stale)."""
    import re
    from pathlib import Path

    doc = Path(__file__).resolve().parent.parent / "docs" / "03-TOOL-SPECS.md"
    pairs = re.findall(r"##\s+`(\w+)\([^`]*\btimeout=(\d+)\)`", doc.read_text(encoding="utf-8"))
    assert pairs, "no timeout-bearing tool headers found in TOOL-SPECS"
    for tool, val in pairs:
        assert int(val) == config.TIMEOUTS[tool], (
            f"{tool}: doc timeout={val} != config {config.TIMEOUTS[tool]}"
        )
