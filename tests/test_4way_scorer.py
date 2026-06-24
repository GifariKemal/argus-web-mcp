"""Offline unit tests for the pure parser/aggregate/render functions in run_4way.py.

No network, no CLI execution, no Argus imports - exercises only the I/O-free helpers
so the 4-way benchmark math is trustworthy before the orchestrator runs the sweep.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import run_4way as r4  # noqa: E402


def test_parse_claude_usage_realistic():
    stdout = (
        '{"usage": {"input_tokens": 31218, "output_tokens": 3, '
        '"cache_read_input_tokens": 20686, "cache_creation_input_tokens": 8220, '
        '"server_tool_use": {"web_search_requests": 4}}, '
        '"total_cost_usd": 0.21, "result": "answer", "num_turns": 1}'
    )
    p = r4.parse_claude_usage(stdout)
    assert p["input_tokens"] == 31218
    assert p["output_tokens"] == 3
    assert p["cache_read_input_tokens"] == 20686
    assert p["cache_creation_input_tokens"] == 8220
    assert p["total_tokens"] == 31221  # input + output
    assert p["cost_usd"] == 0.21
    assert p["num_turns"] == 1
    assert p["web_search_requests"] == 4
    assert p["answer_text"] == "answer"


def test_parse_claude_usage_robust_to_garbage():
    p = r4.parse_claude_usage("not json at all")
    assert p["total_tokens"] is None
    assert p["input_tokens"] is None
    assert p["answer_text"] == ""
    # Missing usage keys -> Nones, never raises.
    p2 = r4.parse_claude_usage('{"result": "hi"}')
    assert p2["total_tokens"] is None
    assert p2["answer_text"] == "hi"
    # Non-object JSON.
    assert r4.parse_claude_usage("[1, 2, 3]")["answer_text"] == ""


def test_parse_codex_tokens_realistic():
    text = (
        "codex\n"
        "Here is the synthesis with https://docs.python.org/3/ as a source.\n"
        "tokens used\n"
        "14,375\n"
        "4"
    )
    p = r4.parse_codex_tokens(text)
    assert p["total_tokens"] == 14375  # comma stripped, number on next line
    assert "synthesis" in p["answer_text"]
    # answer must not include the tokens-used trailer.
    assert "tokens used" not in p["answer_text"]
    assert "14,375" not in p["answer_text"]


def test_parse_codex_tokens_inline_number_and_robustness():
    inline = "codex\nsome answer\ntokens used 9,001"
    p = r4.parse_codex_tokens(inline)
    assert p["total_tokens"] == 9001
    assert p["answer_text"] == "some answer"
    # No tokens line / empty -> None, "".
    assert r4.parse_codex_tokens("codex\njust text")["total_tokens"] is None
    empty = r4.parse_codex_tokens("")
    assert empty["total_tokens"] is None
    assert empty["answer_text"] == ""


def test_count_urls_and_found():
    text = "see https://a.com/x and https://b.org/y and https://a.com/x again"
    assert r4.count_urls(text) == 2  # distinct
    assert r4.found(text) is True  # has urls
    assert r4.found("") is False
    assert r4.found("short") is False  # no url, <=40 chars
    long_prose = "x" * 41
    assert r4.found(long_prose) is True  # >40 chars even without a url


def test_aggregate_condition_skips_none_tokens():
    recs = [
        {"total_tokens": 1000, "cost_usd": 0.10, "latency_s": 5.0, "urls": 3,
         "words": 80, "found": True},
        {"total_tokens": 3000, "cost_usd": None, "latency_s": 7.0, "urls": 1,
         "words": 40, "found": True},
        {"total_tokens": None, "cost_usd": None, "latency_s": 6.0, "urls": 0,
         "words": 0, "found": False, "error": "timeout"},
    ]
    a = r4.aggregate_condition(recs)
    assert a["n"] == 3
    assert a["found_count"] == 2
    # mean/median over the 2 present token values only.
    assert a["mean_total_tokens"] == 2000.0
    assert a["median_total_tokens"] == 2000.0
    # cost mean over the single present cost.
    assert a["mean_cost_usd"] == 0.1
    assert a["mean_latency_s"] == 6.0
    assert a["mean_urls"] == round(4 / 3, 4)
    assert a["mean_answer_words"] == 40.0


def test_aggregate_condition_all_none_tokens():
    recs = [{"total_tokens": None, "found": False}, {"total_tokens": None, "found": False}]
    a = r4.aggregate_condition(recs)
    assert a["mean_total_tokens"] is None
    assert a["median_total_tokens"] is None
    assert a["mean_cost_usd"] is None
    assert a["found_count"] == 0


def test_render_report_has_all_sections_and_delta():
    by_condition = {
        "claude-native": {"n": 1, "found_count": 1, "mean_total_tokens": 50000.0,
                          "median_total_tokens": 50000.0, "mean_cost_usd": 0.3,
                          "mean_latency_s": 20.0, "mean_urls": 4.0,
                          "mean_answer_words": 90.0},
        "claude-argus": {"n": 1, "found_count": 1, "mean_total_tokens": 30000.0,
                         "median_total_tokens": 30000.0, "mean_cost_usd": 0.2,
                         "mean_latency_s": 15.0, "mean_urls": 6.0,
                         "mean_answer_words": 120.0},
        "codex-native": {"n": 1, "found_count": 1, "mean_total_tokens": 14000.0,
                         "median_total_tokens": 14000.0, "mean_cost_usd": None,
                         "mean_latency_s": 18.0, "mean_urls": 3.0,
                         "mean_answer_words": 70.0},
        "codex-argus": {"n": 1, "found_count": 0, "mean_total_tokens": None,
                        "median_total_tokens": None, "mean_cost_usd": None,
                        "mean_latency_s": 12.0, "mean_urls": 0.0,
                        "mean_answer_words": 0.0},
    }
    rows = [
        {"id": "c01-01", "condition": "claude-native", "total_tokens": 50000,
         "cost_usd": 0.3, "latency_s": 20.0, "urls": 4, "words": 90, "found": True,
         "error": None},
        {"id": "c01-01", "condition": "codex-argus", "total_tokens": None,
         "cost_usd": None, "latency_s": 12.0, "urls": 0, "words": 0, "found": False,
         "error": "mcp wiring failed"},
    ]
    report = r4.render_report(by_condition, rows)
    assert "Per-condition leaderboard" in report
    assert "WITH vs WITHOUT Argus" in report
    assert "Per-scenario" in report
    # Claude Argus uses 40% fewer tokens than native -> -40.0%.
    assert "-40.0%" in report
    # None metrics render as '-' (codex-argus tokens), not "None".
    assert "None" not in report
    # All four conditions appear in the leaderboard.
    for cond in by_condition:
        assert cond in report


def test_argus_url_and_token_parsing():
    argus = {
        "type": "http",
        "url": "https://argus.example.xyz/mcp",
        "headers": {"Authorization": "Bearer secret-token-123"},
    }
    url, token = r4._argus_url_and_token(argus)
    assert url == "https://argus.example.xyz/mcp"
    assert token == "secret-token-123"
    # No auth header -> token None, no crash.
    url2, token2 = r4._argus_url_and_token({"url": "http://x/mcp"})
    assert url2 == "http://x/mcp"
    assert token2 is None


def test_build_command_shapes():
    argus = {
        "type": "http",
        "url": "https://argus.example.xyz/mcp",
        "headers": {"Authorization": "Bearer tok"},
    }
    prompt = "research X"
    native = r4._build_command("claude-native", prompt, None)
    assert native[0] == "claude"
    assert "WebSearch" in native and "WebFetch" in native
    assert "--strict-mcp-config" in native

    cargus = r4._build_command("claude-argus", prompt, argus)
    assert "mcp__argus__research" in cargus
    assert "WebSearch" not in cargus

    cx_native = r4._build_command("codex-native", prompt, None)
    assert cx_native[:2] == ["codex", "exec"]
    assert "web_search=live" in cx_native

    cx_argus = r4._build_command("codex-argus", prompt, argus)
    assert any("mcp_servers.argus.url=" in a for a in cx_argus)
    assert any("mcp_servers.argus.bearer_token=tok" in a for a in cx_argus)
