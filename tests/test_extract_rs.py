"""rs-trafilatura subprocess (extract/rs.py) and the page-type routing in extract_article.

The suite runs with ARGUS_RS_TRAFILATURA=off (conftest); these tests fake the binary and
its subprocess, except the last one, which runs the real binary when it is installed.
"""

import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from argus.extract import rs
from argus.extract.article import extract_article
from argus.fetch.static import MAX_FETCH_BYTES
from conftest import ARTICLE_HTML

URL = "https://forum.example.com/t/1"
RS_TEXT = "Thread opener asks a question.\nFirst reply answers it."


def _out(page_type="forum", confidence=0.91, text=RS_TEXT) -> bytes:
    return json.dumps({"title": "t", "main_content": text, "page_type": page_type,
                       "confidence": confidence}).encode()


@pytest.fixture
def fake(monkeypatch):
    """rs.binary() -> a fake path and rs.subprocess.run -> a recorder returning ``state``."""
    state = SimpleNamespace(stdout=_out(), returncode=0, raise_=None, calls=[])

    def run(args, **kw):
        state.calls.append((args, kw))
        if state.raise_:
            raise state.raise_
        return subprocess.CompletedProcess(args, state.returncode, stdout=state.stdout,
                                           stderr=b"")

    monkeypatch.setattr(rs, "binary", lambda: "/usr/local/bin/rs-trafilatura")
    monkeypatch.setattr(rs, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    return state


# ---------------------------------------------------------------- rs.extract


def test_extract_parses_output_and_spawns_safely(fake):
    assert rs.extract("<p>hi</p>", URL) == {
        "page_type": "forum", "confidence": 0.91, "text": RS_TEXT}
    args, kw = fake.calls[0]
    assert args == ["/usr/local/bin/rs-trafilatura", "--url", URL]  # argv list, no shell
    assert kw["input"] == b"<p>hi</p>"
    assert kw["timeout"] == rs.TIMEOUT_S
    assert "shell" not in kw
    assert set(kw["env"]) <= {"SYSTEMROOT"}  # minimal environment


def test_extract_without_url_passes_no_flag(fake):
    rs.extract("<p>hi</p>", "")
    assert fake.calls[0][0] == ["/usr/local/bin/rs-trafilatura"]


@pytest.mark.parametrize("state", [
    {"returncode": 1},
    {"stdout": b"not json"},
    {"stdout": b"[1, 2]"},
    {"stdout": json.dumps({"main_content": 5, "confidence": 0.9}).encode()},
    {"stdout": json.dumps({"main_content": "x", "confidence": "high"}).encode()},
    {"stdout": _out(text="   ")},  # rs found nothing: Argus's own tiers decide
    {"raise_": subprocess.TimeoutExpired(["rs"], rs.TIMEOUT_S)},
    {"raise_": FileNotFoundError("rs-trafilatura")},
    {"raise_": PermissionError("not executable")},
], ids=["exit1", "malformed", "not-object", "text-not-str", "conf-not-num", "empty",
        "timeout", "vanished", "not-executable"])
def test_extract_failure_is_none(fake, state):
    vars(fake).update(state)
    assert rs.extract("<p>hi</p>", URL) is None


def test_extract_skips_missing_binary(fake, monkeypatch):
    monkeypatch.setattr(rs, "binary", lambda: None)
    assert rs.extract("<p>hi</p>", URL) is None
    assert fake.calls == []


def test_extract_skips_oversize_input(fake, monkeypatch):
    monkeypatch.setattr(rs, "MAX_INPUT_BYTES", 10)
    assert rs.extract("<p>" + "é" * 5 + "</p>", URL) is None  # 17 bytes as UTF-8
    assert fake.calls == []


def test_input_cap_is_the_fetch_cap():
    assert rs.MAX_INPUT_BYTES == MAX_FETCH_BYTES


@pytest.mark.parametrize(("env", "which_arg"), [
    (None, "rs-trafilatura"), ("", "rs-trafilatura"), ("/opt/rs/extract", "/opt/rs/extract")])
def test_binary_lookup(monkeypatch, env, which_arg):
    seen = []
    monkeypatch.setattr(rs.shutil, "which", lambda name: seen.append(name) or "/found")
    if env is None:
        monkeypatch.delenv("ARGUS_RS_TRAFILATURA", raising=False)
    else:
        monkeypatch.setenv("ARGUS_RS_TRAFILATURA", env)
    rs.binary.cache_clear()
    try:
        assert rs.binary() == "/found"
        assert seen == [which_arg]
    finally:
        rs.binary.cache_clear()


def test_binary_off(monkeypatch):
    monkeypatch.setenv("ARGUS_RS_TRAFILATURA", "OFF")
    rs.binary.cache_clear()
    try:
        assert rs.binary() is None
    finally:
        rs.binary.cache_clear()


# ---------------------------------------------------------------- routing


@pytest.mark.parametrize("page_type", ["forum", "collection", "listing", "service", "product"])
@pytest.mark.parametrize("fmt", ["markdown", "text"])
def test_non_article_types_use_rs_text(fake, page_type, fmt):
    fake.stdout = _out(page_type=page_type)
    res = extract_article(ARTICLE_HTML, URL, fmt=fmt)
    assert res["content"] == RS_TEXT
    assert res["format"] == fmt
    m = res["metadata"]
    assert (m["extractor"], m["page_type"], m["extraction_quality"]) == (
        "rs-trafilatura", page_type, 0.91)
    assert "low_extraction_quality" not in m
    assert m["word_count"] == len(RS_TEXT.split())
    assert res["title"] == "Gold Outlook"  # metadata still comes from Argus


@pytest.mark.parametrize("page_type", ["article", "documentation", None, "unknown"])
def test_article_types_keep_argus_content(fake, page_type):
    fake.stdout = _out(page_type=page_type)
    res = extract_article(ARTICLE_HTML, URL)
    assert "Central bank demand" in res["content"]
    assert RS_TEXT not in res["content"]
    m = res["metadata"]
    assert "extractor" not in m
    assert (m["page_type"], m["extraction_quality"]) == (page_type, 0.91)


@pytest.mark.parametrize("kw", [{"fmt": "html"}, {"include_links": True}, {"clean": False}],
                         ids=["html", "links", "unclean"])
def test_formats_rs_cannot_serve_keep_argus_content(fake, kw):
    res = extract_article(ARTICLE_HTML, URL, **kw)
    assert RS_TEXT not in res["content"]
    assert "extractor" not in res["metadata"]
    assert res["metadata"]["page_type"] == "forum"


def test_low_confidence_is_flagged(fake):
    fake.stdout = _out(confidence=0.42)
    m = extract_article(ARTICLE_HTML, URL)["metadata"]
    assert m["extraction_quality"] == 0.42
    assert m["low_extraction_quality"] is True


def test_rs_failure_is_todays_result(fake, monkeypatch):
    fake.returncode = 1
    with_failed_rs = extract_article(ARTICLE_HTML, URL)
    monkeypatch.setattr(rs, "binary", lambda: None)
    assert with_failed_rs == extract_article(ARTICLE_HTML, URL)
    assert not {"page_type", "extraction_quality", "extractor"} & set(
        with_failed_rs["metadata"])


def test_empty_html_skips_rs(fake):
    assert extract_article("", URL)["content"] == ""
    assert fake.calls == []


# ---------------------------------------------------------------- the real binary


def _real_binary() -> str | None:
    env = os.environ.get("ARGUS_RS_TRAFILATURA", "")
    if env and env.lower() != "off":
        return shutil.which(env)
    return shutil.which("rs-trafilatura")


@pytest.mark.skipif(_real_binary() is None, reason="rs-trafilatura not installed")
def test_real_binary(monkeypatch):
    monkeypatch.setenv("ARGUS_RS_TRAFILATURA", _real_binary())
    rs.binary.cache_clear()
    try:
        out = rs.extract(ARTICLE_HTML, "https://news.example.com/gold")
        assert out is not None
        assert out["page_type"] in {"article", "forum", "collection", "listing", "service",
                                    "product", "documentation"}
        assert 0.0 <= out["confidence"] <= 1.0
        assert "Central bank demand" in out["text"]
        assert extract_article(ARTICLE_HTML, "https://news.example.com/gold")[
            "metadata"]["page_type"] == out["page_type"]
    finally:
        rs.binary.cache_clear()


def test_rs_is_spawned_with_ocr_child_limits(fake, monkeypatch):
    """On Linux the child dies with its worker and has a memory cap, like tesseract."""
    marker = object()
    from argus.extract import ocr

    monkeypatch.setattr(ocr, "_child_kwargs", lambda: {"env": {"X": "1"}, "preexec_fn": marker})
    rs.extract("<p>hi</p>", URL)
    _, kw = fake.calls[0]
    assert kw["preexec_fn"] is marker
    assert set(kw["env"]) <= {"SYSTEMROOT"}  # still rs's own minimal environment
