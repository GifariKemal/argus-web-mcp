"""Tests for the deterministic query->domain router (pure, offline).

No network, no LLM, no randomness exercised here - the classifier must be a pure
function of its input string.
"""

import inspect

import pytest

from argus.router import ROUTES, classify

# --- shape & contract ---------------------------------------------------------


def test_classify_returns_contract_shape():
    out = classify("transformer attention mechanism paper")
    assert set(out) == {"route", "reason", "scores"}
    assert out["route"] in ROUTES
    assert isinstance(out["reason"], str) and out["reason"]
    assert isinstance(out["scores"], dict)
    assert set(out["scores"]) == set(ROUTES)
    assert all(isinstance(v, int) for v in out["scores"].values())


def test_winning_route_has_max_score():
    out = classify("python asyncio semaphore how to")
    scores = out["scores"]
    assert scores[out["route"]] == max(scores.values())


# --- scholar ------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "transformer attention mechanism paper",
        "survey of diffusion models arxiv",
        "peer-reviewed study on protein folding",
        "literature review of reinforcement learning",
        "smith et al 2022 citation doi",
    ],
)
def test_scholar_queries(query):
    assert classify(query)["route"] == "scholar"


# --- github -------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "fastmcp github repository",
        "open source library for pdf parsing",
        "npm package for date formatting",
        "source code of redis implementation",
        "clone the official sdk repo",
    ],
)
def test_github_queries(query):
    assert classify(query)["route"] == "github"


# --- news ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "latest federal reserve decision 2026",
        "gold price today",
        "breaking news on the election",
        "what was just released by apple this week",
        "recent announcement march 2025",
    ],
)
def test_news_queries(query):
    assert classify(query)["route"] == "news"


# --- it -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "python asyncio semaphore how to",
        "fix typescript type error",
        "rust borrow checker compile error",
        "how to install postgresql on ubuntu",
        "debug segmentation fault in c++",
    ],
)
def test_it_queries(query):
    assert classify(query)["route"] == "it"


# --- general / default --------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "best pizza in jakarta",
        "",
        "   ",
        "things to do on a rainy afternoon",
        "blue whale facts",
    ],
)
def test_general_queries(query):
    assert classify(query)["route"] == "general"


def test_no_signal_scores_all_zero_and_general():
    out = classify("best pizza in jakarta")
    assert out["route"] == "general"
    # only the 'general' baseline (0) - no real route scored
    assert out["scores"]["github"] == 0
    assert out["scores"]["scholar"] == 0
    assert out["scores"]["news"] == 0
    assert out["scores"]["it"] == 0


# --- determinism --------------------------------------------------------------


def test_deterministic_same_input_same_output():
    q = "survey of diffusion models arxiv 2025"
    first = classify(q)
    second = classify(q)
    assert first == second


def test_deterministic_across_many_repeats():
    q = "python how to fix error in github repository"
    results = [classify(q) for _ in range(20)]
    assert all(r == results[0] for r in results)


# --- purity: no network / llm imports -----------------------------------------


def test_module_is_pure_no_forbidden_imports():
    src = inspect.getsource(__import__("argus.router", fromlist=["x"]))
    forbidden = (
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "socket",
        "openai",
        "anthropic",
        "random",
        "fastembed",
    )
    for name in forbidden:
        assert f"import {name}" not in src, f"router must not import {name}"


# --- tiebreak rule ------------------------------------------------------------
# Tiebreak priority on EXACTLY equal top scores: scholar > github > news > it > general.
# This favors the higher-precision, intent-heavy routes over broad ones.


def test_tiebreak_scholar_beats_news():
    # 'study' (scholar) and 'today' (news) each fire one strong-ish signal; if they
    # tie at the top, scholar wins by priority.
    out = classify("study today")
    assert out["route"] == "scholar"
    # confirm it was actually a tie at the top (scholar and news both at the max)
    top = max(out["scores"].values())
    assert out["scores"]["scholar"] == top
    assert out["scores"]["news"] == top


def test_tiebreak_github_beats_it():
    # 'sdk' (github) vs 'install' (it), both weight 2 -> exact tie, github wins by priority.
    out = classify("sdk install")
    assert out["route"] == "github"
    top = max(out["scores"].values())
    assert out["scores"]["github"] == top
    assert out["scores"]["it"] == top


def test_tiebreak_news_beats_it():
    # 'today' (news) vs 'config' (it). On an exact tie, news wins.
    out = classify("today config")
    assert out["route"] == "news"
    top = max(out["scores"].values())
    assert out["scores"]["news"] == top
    assert out["scores"]["it"] == top
