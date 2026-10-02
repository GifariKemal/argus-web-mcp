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

import json
import logging
from bisect import bisect_right
from typing import Any

import lxml.html
import trafilatura
from markdownify import markdownify as _md
from readability import Document

# readability log.exception()s a full traceback on every empty page (a blocked fetch)
# before raising; we already catch that and fall through, so the traceback is noise.
logging.getLogger("readability").setLevel(logging.CRITICAL)

_THIN_WORDS = 150  # below this a precision-pass result is retried balanced


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
    # A run of length L at p can only repeat if keys[p + L] == keys[p], so try only the
    # later occurrences of keys[p] instead of every L (O(n^3) -> ~linear: a catalog page
    # yields ~4000 blocks and took 16 s here).
    occ: dict[str, list[int]] = {}
    for i, k in enumerate(keys):
        occ.setdefault(k, []).append(i)
    drop: set[int] = set()  # positions within `idx` to drop
    p = 0
    while p < n:
        o = occ[keys[p]]
        for q in reversed(o[bisect_right(o, p) : bisect_right(o, p + (n - p) // 2)]):
            length = q - p  # longest adjacent repeat first
            # cheap last-element check before the O(L) slice compare
            if keys[q - 1] == keys[q + length - 1] and keys[p:q] == keys[q : q + length]:
                drop.update(range(q, q + length))
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


def _jsonld(tree: Any) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page, flattening lists and ``@graph``; bad JSON skipped."""
    out: list[dict[str, Any]] = []
    for raw in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            stack = [json.loads(raw)]
        except ValueError:
            continue
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                out.append(d)
                stack.append(d.get("@graph"))
    return out


def _name(v: Any) -> Any:
    """schema.org Person/Brand/list -> its name (first one for a list)."""
    if isinstance(v, list):
        v = v[0] if v else None
    return v.get("name") if isinstance(v, dict) else v


def _text(v: Any) -> str | None:
    return None if v is None or isinstance(v, (dict, list)) else str(v)


def _apply_jsonld(meta: dict[str, Any], tree: Any) -> None:
    for d in _jsonld(tree):
        t = d.get("@type")
        # @type may be a list or (malformed pages) a dict; only strings can match.
        types = {x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}
        if "Product" in types and "structured" not in meta:
            offer = d.get("offers")
            offer = (offer[0] if offer else {}) if isinstance(offer, list) else offer
            offer = offer if isinstance(offer, dict) else {}
            fields = {
                "type": "Product",
                "name": d.get("name"),
                "description": d.get("description"),
                "sku": d.get("sku"),
                "brand": _name(d.get("brand")),
                "price": _text(offer.get("price") or offer.get("lowPrice")),
                "priceCurrency": offer.get("priceCurrency"),
                "availability": str(offer.get("availability") or "").rsplit("/", 1)[-1],
            }
            meta["structured"] = {k: v for k, v in fields.items() if v not in (None, "")}
        elif types & {"Article", "NewsArticle", "BlogPosting"}:
            if not meta["published"] and isinstance(d.get("datePublished"), str):
                meta["published"] = d["datePublished"][:10]  # htmldate's YYYY-MM-DD
            meta["author"] = meta["author"] or _text(_name(d.get("author")))


def _metadata(html: str, url: str) -> dict[str, Any]:
    # extract_metadata parses ONLY the head/metadata (~7x cheaper than bare_extraction,
    # which extracted the full body a second time just to read these five fields).
    # extensive=False: the extensive htmldate search ran dateparser on every node (31 s
    # on a 340 KB listing) and invented dates from "Copyright 2019" footers; meta tags
    # and JSON-LD dates are still read.
    try:
        m = trafilatura.extract_metadata(html, default_url=url, extensive=False)
    except Exception:
        m = None
    meta = {
        "title": getattr(m, "title", None),
        "author": getattr(m, "author", None),
        "published": getattr(m, "date", None),
        "lang": getattr(m, "language", None),
        "site": getattr(m, "sitename", None) or getattr(m, "hostname", None),
    }
    try:
        tree = lxml.html.fromstring(html)
    except Exception:  # empty / unparseable document
        return meta
    meta["lang"] = meta["lang"] or (
        tree.xpath("string(/html/@lang)")
        or tree.xpath(
            "string(//meta[translate(@http-equiv,'CONTENTLANGUAGE','contentlanguage')"
            "='content-language']/@content)"
        )
    ).strip() or None
    try:
        _apply_jsonld(meta, tree)
    except Exception:  # noqa: BLE001, S110 - page-authored JSON-LD must never fail the read
        pass
    return meta


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
    def _tier1(precision: bool) -> str:
        return _dedup_blocks(
            (
                trafilatura.extract(
                    html,
                    url=url,
                    output_format="markdown",
                    include_links=include_links,
                    with_metadata=False,
                    include_comments=False,  # comment threads (Reddit/HN/Disqus) are noise
                    favor_precision=precision,
                )
                or ""
            ).strip()
        )

    content = _tier1(clean)
    # Precision keeps only the "article" and drops the rest of a non-article page (a
    # forum thread came back as its heading alone). When that result is thin, take a
    # balanced pass instead - but only if it recovers clearly more text.
    words = len(content.split())
    if clean and words < _THIN_WORDS:
        balanced = _tier1(False)
        if len(balanced.split()) > 2 * words:
            content = balanced

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
