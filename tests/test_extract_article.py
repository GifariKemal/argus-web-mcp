"""Tests for argus.extract.article - tiered article extraction.

Tiers:
  1. trafilatura (primary)
  2. readability-lxml -> markdownify (fallback)
  3. markdownify(html) (last resort)
Whatever real text the tiers recover is returned, however short. content == ""
only when no tier extracted any text at all (browser escalation for thin pages
is the fetch layer's job, upstream on the raw html).
"""

from pathlib import Path

import pytest

from argus.extract import article as art
from argus.extract.article import extract_article

FIXTURES = Path(__file__).parent / "fixtures"
AD_HEAVY = (FIXTURES / "ad_heavy_news.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- tier 1: trafilatura


def test_ad_heavy_news_strips_chrome_keeps_article():
    res = extract_article(AD_HEAVY, "http://news.example.com/cb-rates")

    assert res["format"] == "markdown"
    content = res["content"]

    # The real article sentences survive.
    assert "Central banks signaled a pause" in content
    assert "inflation to moderate" in content
    assert "Equity markets responded positively" in content

    # Navigation / ads / cookie banner / footer chrome is stripped.
    assert "Login Register Subscribe" not in content
    assert "SUPER SALE" not in content
    assert "buy cheap watches" not in content
    assert "Accept all cookies" not in content
    assert "Promoted content sponsored" not in content
    assert "All rights reserved" not in content

    assert res["title"]  # title present
    assert "Central Banks" in res["title"]
    assert res["metadata"]["word_count"] > 0
    assert res["metadata"]["author"] == "Jane Doe"
    assert res["metadata"]["site"]  # sitename/hostname populated


def test_no_duplicate_blocks_in_markdown():
    # trafilatura 2.0 + favor_precision duplicates blocks; the extractor must de-dup.
    res = extract_article(AD_HEAVY, "http://news.example.com/cb-rates")
    assert res["content"].count("Central banks signaled a pause") == 1


def test_word_count_matches_split():
    res = extract_article(AD_HEAVY, "http://news.example.com/cb-rates")
    assert res["metadata"]["word_count"] == len(res["content"].split())


def test_text_format_has_no_markdown_heading_marks():
    res = extract_article(AD_HEAVY, "http://news.example.com/cb-rates", fmt="text")
    assert res["format"] == "text"
    assert not res["content"].lstrip().startswith("#")
    assert "Central banks signaled a pause" in res["content"]


def test_html_format_returns_markup():
    res = extract_article(AD_HEAVY, "http://news.example.com/cb-rates", fmt="html")
    assert res["format"] == "html"
    assert "<" in res["content"] and ">" in res["content"]
    assert "Central banks signaled a pause" in res["content"]


# ---------------------------------------------------------------- tier 2: readability fallback


def test_readability_fallback_when_trafilatura_empty(monkeypatch):
    """Force tier-1 to yield nothing; readability must still recover the body."""
    monkeypatch.setattr(art.trafilatura, "extract", lambda *a, **k: None)

    html = (
        "<html><head><title>Fallback Title</title></head><body>"
        "<section><div class='story'>"
        "<p>Readability recovers this meaningful paragraph of article text "
        "for the fallback test here.</p>"
        "<p>A second full sentence ensures the recovered content is a "
        "substantial body of real text.</p>"
        "<p>A third sentence adds even more words so the extracted body stays "
        "well above the limit.</p>"
        "</div></section></body></html>"
    )
    res = extract_article(html, "http://x.test/a")
    assert "Readability recovers this meaningful paragraph" in res["content"]
    assert res["metadata"]["word_count"] >= 25
    # title still resolved (readability Document.title()).
    assert res["title"]


# ---------------------------------------------------------------- tier 3: markdownify last resort


def test_markdownify_last_resort(monkeypatch):
    """Both trafilatura and readability yield nothing -> raw markdownify of html."""
    monkeypatch.setattr(art.trafilatura, "extract", lambda *a, **k: None)

    def _boom(html):  # readability tier raises / yields empty
        return ""

    monkeypatch.setattr(art, "_readability_markdown", _boom)

    html = (
        "<html><body><p>Last resort markdownify keeps every visible word of the "
        "document body intact so the integrator always gets something usable here today "
        "no matter how broken the upstream extraction tiers turned out to be in practice.</p>"
        "</body></html>"
    )
    res = extract_article(html, "http://x.test/lr")
    assert "Last resort markdownify keeps every visible word" in res["content"]
    assert res["metadata"]["word_count"] >= 25


# ---------------------------------------------------------------- empty vs short-but-real


def test_truly_empty_html_yields_empty_content():
    # No text anywhere -> all tiers extract nothing -> content "".
    res = extract_article("<html><head></head><body></body></html>", "http://x.test/empty")
    assert res["content"] == ""
    assert res["metadata"]["word_count"] == 0


def test_whitespace_only_html_yields_empty_content():
    res = extract_article(
        "<html><body>   \n\t  <p>   </p>  </body></html>", "http://x.test/ws"
    )
    assert res["content"] == ""
    assert res["metadata"]["word_count"] == 0


def test_short_real_page_is_preserved_not_blanked():
    # A genuinely short article must be RETURNED, not discarded.
    html = (
        "<html><body><article><p>This domain is for use in documentation "
        "examples without needing permission.</p></article></body></html>"
    )
    res = extract_article(html, "http://x.test/short")
    assert "documentation examples" in res["content"]
    assert res["metadata"]["word_count"] > 0


def test_one_word_real_page_is_preserved(monkeypatch):
    # Even a single recovered word is real content; do not blank it.
    monkeypatch.setattr(art.trafilatura, "extract", lambda *a, **k: "hi")
    res = extract_article("<html><body><p>hi</p></body></html>", "http://x.test/one")
    assert res["content"] == "hi"
    assert res["metadata"]["word_count"] == 1


def test_example_com_like_short_page_regression():
    # Regression: example.com extracts to ~17 words of real content. Previously the
    # 25-word thin blanking discarded it, making the `read` tool wrongly report
    # empty_content. It must now be preserved.
    html = (
        "<html><head><title>Example Domain</title></head><body>"
        "<div><h1>Example Domain</h1>"
        "<p>This domain is for use in illustrative examples in documents. You may "
        "use this domain in literature without prior coordination or asking for "
        "permission.</p>"
        "<p><a href='https://www.iana.org/domains/example'>More information...</a></p>"
        "</div></body></html>"
    )
    res = extract_article(html, "http://example.com/")
    assert res["content"] != ""
    assert "illustrative examples" in res["content"]
    assert 0 < res["metadata"]["word_count"] < 30  # short, but kept


# ---------------------------------------------------------------- links toggle


def test_include_links_toggle():
    html = (
        "<html><head><title>Links</title></head><body><article>"
        "<p>Read the full <a href='http://dest.example/x'>report here</a> for the "
        "complete coverage of the central bank policy decision announced this week.</p>"
        "<p>Additional supporting analysis follows in the next several paragraphs below now.</p>"
        "</article></body></html>"
    )
    with_links = extract_article(html, "http://x.test/l", include_links=True)
    without = extract_article(html, "http://x.test/l", include_links=False)
    assert "http://dest.example/x" in with_links["content"]
    assert "http://dest.example/x" not in without["content"]


def test_return_shape_keys():
    res = extract_article(AD_HEAVY, "http://x.test/a")
    assert set(res.keys()) == {"content", "format", "title", "metadata"}
    assert set(res["metadata"].keys()) == {
        "author",
        "published",
        "lang",
        "site",
        "word_count",
    }


@pytest.mark.parametrize("fmt", ["markdown", "text", "html"])
def test_short_real_content_preserved_across_formats(fmt):
    html = (
        "<html><body><article><p>This domain is for use in documentation "
        "examples without needing permission.</p></article></body></html>"
    )
    res = extract_article(html, "http://x/t", fmt=fmt)
    assert res["format"] == fmt
    assert res["content"] != ""
    assert "documentation examples" in res["content"]


@pytest.mark.parametrize("fmt", ["markdown", "text", "html"])
def test_truly_empty_blank_across_formats(fmt):
    res = extract_article("<html><body></body></html>", "http://x/t", fmt=fmt)
    assert res["content"] == ""
    assert res["format"] == fmt
