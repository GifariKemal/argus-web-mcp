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


def test_html_format_falls_back_when_readability_raises(monkeypatch):
    # fmt='html' converts via readability Document(html).summary(); if that raises on
    # malformed input, _to_format must fall back to the markdown content (no crash).
    monkeypatch.setattr(art.trafilatura, "extract",
                        lambda *a, **k: "Recovered article body of real text here.")

    class _Boom:
        def __init__(self, *a, **k):
            raise ValueError("malformed document")

    monkeypatch.setattr(art, "Document", _Boom)

    res = extract_article("<html><body><<broken>>", "http://x.test/m", fmt="html")
    assert res["format"] == "html"
    # falls back to the markdown content rather than raising.
    assert "Recovered article body" in res["content"]


def test_html_format_malformed_input_does_not_crash():
    # End-to-end: genuinely malformed markup through the html path must not raise.
    res = extract_article("<html><body><p>unterminated <b>bold", "http://x.test/m2",
                          fmt="html")
    assert res["format"] == "html"
    assert isinstance(res["content"], str)


@pytest.mark.parametrize("fmt", ["markdown", "text", "html"])
def test_truly_empty_blank_across_formats(fmt):
    res = extract_article("<html><body></body></html>", "http://x/t", fmt=fmt)
    assert res["content"] == ""
    assert res["format"] == fmt


# --- _dedup_blocks: consecutive-only (docstring contract) ----------------------


def test_dedup_keeps_non_adjacent_repeats():
    """A refrain repeated later in the document is real content, not extractor noise."""
    from argus.extract.article import _dedup_blocks

    text = "Chorus line repeated.\n\nVerse one text here.\n\nChorus line repeated.\n\nVerse two."
    out = _dedup_blocks(text)
    assert out.count("Chorus line repeated.") == 2


def test_dedup_collapses_adjacent_repeats():
    from argus.extract.article import _dedup_blocks

    assert _dedup_blocks("Same block.\n\nSame block.\n\nNext.").count("Same block.") == 1
    # a blank block between the pair must not defeat the collapse
    assert _dedup_blocks("Same block.\n\n\n\nSame block.").count("Same block.") == 1


def test_dedup_collapses_whole_body_run_after_heading():
    """trafilatura 2.x signature: the multi-block body repeats verbatim after the title.

    [# Title, A, B, A, B] must collapse to [# Title, A, B] - the adjacent-only dedup
    (key == prev) could never catch this because the repeated blocks are NON-adjacent.
    """
    from argus.extract.article import _dedup_blocks

    out = _dedup_blocks("# Title\n\nAlpha para.\n\nBeta para.\n\nAlpha para.\n\nBeta para.")
    assert out.count("Alpha para.") == 1
    assert out.count("Beta para.") == 1
    assert out.count("# Title") == 1
    assert out == "# Title\n\nAlpha para.\n\nBeta para."  # order preserved


def test_dedup_collapses_full_document_repeat():
    from argus.extract.article import _dedup_blocks

    assert _dedup_blocks("A one.\n\nB two.\n\nA one.\n\nB two.") == "A one.\n\nB two."


def _dedup_reference(text: str) -> str:
    """The original O(n^3) _dedup_blocks, kept verbatim as the behavioural oracle."""
    blocks = text.split("\n\n")
    idx = [i for i, b in enumerate(blocks) if b.strip()]
    keys = [blocks[i].strip() for i in idx]
    n = len(keys)
    drop: set[int] = set()
    p = 0
    while p < n:
        for length in range((n - p) // 2, 0, -1):
            if keys[p : p + length] == keys[p + length : p + 2 * length]:
                drop.update(range(p + length, p + 2 * length))
                p += 2 * length
                break
        else:
            p += 1
    if not drop:
        return text
    drop_orig = {idx[k] for k in drop}
    return "\n\n".join(b for i, b in enumerate(blocks) if i not in drop_orig)


def test_dedup_matches_reference_on_random_inputs():
    import random

    from argus.extract.article import _dedup_blocks

    rng = random.Random(1234)
    for _ in range(3000):
        # tiny alphabet + blanks so adjacent runs, nested runs and refrains all occur
        blocks = [rng.choice(["A", "B", "C", " A ", "", "  "]) for _ in range(rng.randint(0, 14))]
        text = "\n\n".join(blocks)
        assert _dedup_blocks(text) == _dedup_reference(text), blocks


def test_dedup_is_near_linear_on_large_block_streams():
    import time

    from argus.extract.article import _dedup_blocks

    distinct = "\n\n".join(f"block {i}" for i in range(4000))  # was 16 s
    refrain = "\n\n".join("Add to cart" if i % 2 else f"item {i}" for i in range(4000))
    t = time.perf_counter()
    assert _dedup_blocks(distinct) == distinct
    assert _dedup_blocks(refrain) == refrain  # non-adjacent repeats are kept
    assert time.perf_counter() - t < 1.0


# --- metadata --------------------------------------------------------------------


def test_copyright_footer_does_not_invent_a_date():
    html = (
        "<html><head><title>T</title></head><body><p>Some real text.</p>"
        "<footer>Copyright 2019 Example Corp</footer></body></html>"
    )
    assert extract_article(html, "http://x.test/c")["metadata"]["published"] is None


def test_meta_tag_date_still_found():
    html = (
        '<html><head><meta property="article:published_time" content="2024-03-05">'
        "</head><body><p>Some real text.</p></body></html>"
    )
    assert extract_article(html, "http://x.test/d")["metadata"]["published"] == "2024-03-05"


def test_lang_from_html_attribute_and_content_language():
    body = "<body><p>Some real text.</p></body></html>"
    tagged = '<html lang="id-ID">' + body
    assert extract_article(tagged, "http://x/l")["metadata"]["lang"] == "id-ID"
    meta = '<html><head><meta http-equiv="Content-Language" content="de"></head>'
    assert extract_article(meta + body, "http://x/l")["metadata"]["lang"] == "de"
    assert extract_article("<html>" + body, "http://x/l")["metadata"]["lang"] is None


def _ld(*objs: str) -> str:
    scripts = "".join(f'<script type="application/ld+json">{o}</script>' for o in objs)
    return f"<html><head>{scripts}</head><body><p>Some real text.</p></body></html>"


def test_jsonld_product_exposed_as_structured():
    product = (
        '{"@context":"https://schema.org","@graph":[{"@type":"WebPage"},'
        '{"@type":"Product","name":"SRT Gateway","sku":"MG-1210","description":"Modbus",'
        '"brand":{"@type":"Brand","name":"SURIOTA"},"offers":[{"@type":"Offer",'
        '"price":"199.00","priceCurrency":"USD","availability":"https://schema.org/InStock"}]}]}'
    )
    meta = extract_article(_ld("{not json", product), "http://x/p")["metadata"]
    assert meta["structured"] == {
        "type": "Product",
        "name": "SRT Gateway",
        "description": "Modbus",
        "sku": "MG-1210",
        "brand": "SURIOTA",
        "price": "199.00",
        "priceCurrency": "USD",
        "availability": "InStock",
    }


def test_jsonld_article_fills_missing_date_and_author():
    article = (
        '[{"@type":"NewsArticle","datePublished":"2024-06-01T08:00:00Z",'
        '"author":[{"@type":"Person","name":"Jane Roe"}]}]'
    )
    meta = extract_article(_ld(article), "http://x/a")["metadata"]
    assert meta["published"] == "2024-06-01"
    assert meta["author"] == "Jane Roe"
    assert "structured" not in meta


def test_malformed_jsonld_is_ignored():
    meta = extract_article(_ld("{oops", '"a string"', '{"@type":"Product","offers":"x"}'),
                           "http://x/m")["metadata"]
    assert meta["structured"] == {"type": "Product"}


# --- thin precision result -> balanced pass ----------------------------------------


def _fake_tier1(monkeypatch, precise: str, balanced: str) -> list:
    calls = []

    def fake_extract(*a, **k):
        calls.append(k["favor_precision"])
        return precise if k["favor_precision"] else balanced

    monkeypatch.setattr(art.trafilatura, "extract", fake_extract)
    return calls


def test_thin_precision_result_falls_back_to_balanced_pass(monkeypatch):
    # forum shape: precision keeps the thread heading and drops the three posts
    posts = "\n\n".join(f"Post {i}: the watchdog reset fixed Modbus polling." for i in range(3))
    _fake_tier1(monkeypatch, "# Gateway thread", "# Gateway thread\n\n" + posts)
    content = extract_article("<html><body><p>x</p></body></html>", "http://f/t")["content"]
    assert all(f"Post {i}:" in content for i in range(3))


def test_thin_precision_kept_when_balanced_adds_little(monkeypatch):
    # a short real article: balanced only adds a nav crumb, precision wins
    body = "Short real article body text."
    _fake_tier1(monkeypatch, body, "Home News\n\n" + body)
    content = extract_article("<html><body><p>x</p></body></html>", "http://f/a")["content"]
    assert content == "Short real article body text."


def test_balanced_pass_only_runs_when_precision_is_thin(monkeypatch):
    long_text = " ".join(["word"] * 200)
    calls = _fake_tier1(monkeypatch, long_text, long_text + " more")
    extract_article("<html><body><p>x</p></body></html>", "http://x/b")
    assert calls == [True]


def test_malformed_jsonld_type_does_not_fail_the_read():
    html = ('<html><head><script type="application/ld+json">'
            '[{"@type": {"x": 1}}, {"@type": ["Article", {"y": 2}], "author": {"name": 7}}]'
            '</script></head><body><article><p>' + "Isi artikel yang cukup panjang. " * 40 +
            '</p></article></body></html>')
    from argus.extract.article import extract_article
    out = extract_article(html, "https://example.com/a")
    assert out["content"]
    assert out["metadata"]["author"] == "7"
