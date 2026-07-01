"""Tiered HTML -> article extraction.

Tier 1  trafilatura      (precision-favouring, primary)
Tier 2  readability-lxml -> markdownify   (fallback when tier 1 is empty)
Tier 3  markdownify(html)                 (last resort)

The extractor returns whatever real content the tiers recover, however short.
``content == ""`` is yielded only when the tiers genuinely extracted no text at
all. Browser escalation for thin pages is owned by the fetch layer (it inspects
the RAW html before extraction), so the extractor must NOT blank short-but-real
pages - doing so silently discarded legitimate content (e.g. example.com).
"""

from __future__ import annotations

from typing import Any

import trafilatura
from markdownify import markdownify as _md
from readability import Document


def _dedup_blocks(text: str) -> str:
    """Drop consecutive duplicate paragraphs.

    trafilatura 2.0 with ``favor_precision=True`` emits each block twice; collapse
    the verbatim repeat while preserving order and intentional repeats elsewhere.
    """
    blocks = text.split("\n\n")
    out: list[str] = []
    seen: set[str] = set()
    for b in blocks:
        key = b.strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(b)
    return "\n\n".join(out)


def _readability_markdown(html: str) -> str:
    """Tier 2: readability summary -> markdown. Returns '' if nothing usable."""
    try:
        summary_html = Document(html).summary()
    except Exception:
        return ""
    if not summary_html:
        return ""
    return _md(summary_html, heading_style="ATX").strip()


def _to_format(content_md: str, fmt: str, html_source: str) -> str:
    """content_md is markdown; convert to the requested output format."""
    if fmt == "markdown":
        return content_md
    if fmt == "text":
        # strip the few markdown marks we emit (headings, links, emphasis).
        text = trafilatura.extract(
            html_source, output_format="txt", with_metadata=False, include_comments=False
        )
        if text:
            return _dedup_blocks(text.replace("\n", "\n\n")).replace("\n\n", "\n").strip()
        # fall back: crude de-mark of the markdown.
        return content_md.replace("#", "").strip()
    if fmt == "html":
        try:
            return Document(html_source).summary()
        except Exception:
            return content_md
    return content_md


def _metadata(html: str, url: str) -> dict[str, Any]:
    try:
        doc = trafilatura.bare_extraction(html, url=url, with_metadata=True)
    except Exception:
        doc = None
    d = doc.as_dict() if doc is not None else {}
    return {
        "title": d.get("title"),
        "author": d.get("author"),
        "published": d.get("date"),
        "lang": d.get("language"),
        "site": d.get("sitename") or d.get("hostname"),
    }


def extract_article(
    html: str,
    url: str,
    fmt: str = "markdown",
    clean: bool = True,
    include_links: bool = False,
) -> dict[str, Any]:
    """Extract the main article from ``html`` into ``fmt`` (markdown|text|html).

    ``clean`` favours precision (less boilerplate). ``include_links`` keeps inline
    links. Returns ``{"content", "format", "title", "metadata"}`` where metadata is
    ``{"author", "published", "lang", "site", "word_count"}``. Whatever real text
    the tiers recover is returned verbatim - even a one-word page. ``content`` is
    ``""`` (``word_count`` 0) only when no tier extracted any text at all.
    """
    meta = _metadata(html, url)

    # Tier 1: trafilatura (content only; metadata fetched separately to avoid the
    # YAML front-matter that with_metadata=True injects into the body).
    content = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=include_links,
        with_metadata=False,
        include_comments=False,  # comment threads (Reddit/HN/Disqus) are boilerplate noise, not main content
        favor_precision=clean,
    )

    # Tier 2: readability.
    if not content:
        content = _readability_markdown(html)

    # Tier 3: raw markdownify.
    if not content:
        content = _md(html, heading_style="ATX").strip()

    content = _dedup_blocks((content or "").strip())

    # Convert to the requested format only when a tier actually recovered text;
    # otherwise the format converters can fabricate empty wrappers (e.g.
    # readability emits ``<body id="readabilityBody"></body>`` for empty input).
    if content and fmt != "markdown":
        content = _to_format(content, fmt, html) or ""

    word_count = len(content.split())

    title = meta.pop("title")
    meta["word_count"] = word_count

    return {"content": content, "format": fmt, "title": title, "metadata": meta}
