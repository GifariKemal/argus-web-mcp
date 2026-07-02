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
    """Drop duplicated blocks emitted by the extractor.

    trafilatura 2.x re-emits the whole ``<article>``/``<main>`` body a second time, so
    the block stream is a contiguous RUN repeated verbatim right after itself - e.g.
    ``[Title, A, B, A, B]`` (the heading sits outside the duplicated run). Collapse the
    longest such adjacent run-duplication anywhere in the stream (a single duplicated
    block is the ``L == 1`` case, preserving the old adjacent-collapse behaviour) while
    keeping genuine non-adjacent repeats - a refrain/legal clause recurring later is real
    content, not extractor noise. Blank blocks are layout and must not defeat a match.
    """
    blocks = text.split("\n\n")
    idx = [i for i, b in enumerate(blocks) if b.strip()]  # non-empty block positions
    keys = [blocks[i].strip() for i in idx]
    n = len(keys)
    drop: set[int] = set()  # positions within `idx` to drop
    p = 0
    while p < n:
        for length in range((n - p) // 2, 0, -1):  # longest adjacent repeat first
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
    # extract_metadata parses ONLY the head/metadata (~7x cheaper than bare_extraction,
    # which extracted the full body a second time just to read these five fields).
    try:
        m = trafilatura.extract_metadata(html, default_url=url)
    except Exception:
        m = None
    return {
        "title": getattr(m, "title", None),
        "author": getattr(m, "author", None),
        "published": getattr(m, "date", None),
        "lang": getattr(m, "language", None),
        "site": getattr(m, "sitename", None) or getattr(m, "hostname", None),
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
        include_comments=False,  # comment threads (Reddit/HN/Disqus) are noise, not content
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
