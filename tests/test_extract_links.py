"""Tests for argus.extract.links - anchor + image extraction (Firecrawl/Jina parity)."""

from argus.extract.links import extract_links_images

BASE = "https://example.com/blog/post"


def test_relative_and_absolute_links_resolved_and_fragment_stripped():
    html = (
        "<a href='/about'>About</a>"
        "<a href='https://other.com/x'>Other</a>"
        "<a href='page#section'>Frag</a>"
        "<a href='mailto:a@b.com'>Mail</a>"
        "<a href='javascript:void(0)'>JS</a>"
        "<a href='tel:+1234'>Tel</a>"
    )
    res = extract_links_images(html, BASE)
    urls = [link["url"] for link in res["links"]]
    assert urls == [
        "https://example.com/about",
        "https://other.com/x",
        "https://example.com/blog/page",
    ]
    assert res["links"][0]["text"] == "About"
    # mailto/javascript/tel dropped
    assert all("mailto:" not in u and "javascript:" not in u and "tel:" not in u for u in urls)


def test_images_resolved_alt_and_data_uri_dropped():
    html = (
        "<img src='/img/a.png' alt='Logo'>"
        "<img src='//cdn.example.net/x.png' alt='CDN'>"
        "<img src='https://full.com/b.jpg'>"
        "<img src='data:image/png;base64,AAAA' alt='inline'>"
    )
    res = extract_links_images(html, BASE)
    srcs = [img["src"] for img in res["images"]]
    assert srcs == [
        "https://example.com/img/a.png",
        "https://cdn.example.net/x.png",
        "https://full.com/b.jpg",
    ]
    assert res["images"][0]["alt"] == "Logo"
    assert res["images"][2]["alt"] == ""  # missing alt -> empty string
    assert all(not s.startswith("data:") for s in srcs)


def test_dedup_and_order_preserved():
    html = (
        "<a href='/first'>1</a>"
        "<a href='/second'>2</a>"
        "<a href='/first'>dup</a>"
        "<img src='/a.png'><img src='/a.png'><img src='/b.png'>"
    )
    res = extract_links_images(html, BASE)
    assert [link["url"] for link in res["links"]] == [
        "https://example.com/first",
        "https://example.com/second",
    ]
    assert [img["src"] for img in res["images"]] == [
        "https://example.com/a.png",
        "https://example.com/b.png",
    ]


def test_same_domain_only_keeps_same_host_and_www_variant():
    html = (
        "<a href='https://example.com/a'>a</a>"
        "<a href='https://www.example.com/b'>b</a>"
        "<a href='https://evil.com/c'>c</a>"
        "<a href='/d'>d</a>"
    )
    res = extract_links_images(html, BASE, same_domain_only=True)
    urls = [link["url"] for link in res["links"]]
    assert urls == [
        "https://example.com/a",
        "https://www.example.com/b",
        "https://example.com/d",
    ]
    assert "https://evil.com/c" not in urls


def test_max_items_cap_sets_truncated_flags():
    html = "".join(f"<a href='/l{i}'>l</a>" for i in range(5))
    html += "".join(f"<img src='/i{i}.png'>" for i in range(5))
    res = extract_links_images(html, BASE, max_items=2)
    assert len(res["links"]) == 2
    assert len(res["images"]) == 2
    assert res["links_truncated"] is True
    assert res["images_truncated"] is True


def test_no_truncation_flags_false_when_under_cap():
    html = "<a href='/x'>x</a><img src='/y.png'>"
    res = extract_links_images(html, BASE, max_items=500)
    assert res["links_truncated"] is False
    assert res["images_truncated"] is False


def test_empty_and_malformed_html_no_raise():
    assert extract_links_images("", BASE) == {
        "links": [],
        "images": [],
        "links_truncated": False,
        "images_truncated": False,
    }
    # malformed / unclosed tags must not raise
    res = extract_links_images("<a href='/x'>oops<img src='/y.png'", BASE)
    assert [link["url"] for link in res["links"]] == ["https://example.com/x"]
    assert [img["src"] for img in res["images"]] == ["https://example.com/y.png"]


def test_anchor_text_trimmed():
    html = "<a href='/x'>  spaced  text  </a>"
    res = extract_links_images(html, BASE)
    assert res["links"][0]["text"] == "spaced  text"


def test_empty_and_whitespace_href_skipped():
    html = "<a href=''>empty</a><a href='   '>ws</a><a href='/ok'>ok</a>"
    res = extract_links_images(html, BASE)
    assert [link["url"] for link in res["links"]] == ["https://example.com/ok"]
