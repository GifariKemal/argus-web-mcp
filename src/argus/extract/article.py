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
import re
from bisect import bisect_right
from typing import Any

import lxml.html
import trafilatura
from markdownify import markdownify as _md
from readability import Document

from .links import pdf_links

# readability log.exception()s a full traceback on every empty page (a blocked fetch)
# before raising; we already catch that and fall through, so the traceback is noise.
logging.getLogger("readability").setLevel(logging.CRITICAL)



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


_RECALL_GAIN = 1.3  # balanced replaces precision only when it has this much more text
_LANG = re.compile(r"(?:^|\s)(?:language|lang|highlight(?:-source)?)-([\w+#.-]+)")
# Wrappers only count through Sphinx's highlight-<lang>: a page-level class such as
# <body class="lang-en"> or "language-selector" is not a code language.
_WRAPPER_LANG = re.compile(r"(?:^|\s)highlight(?:-source)?-([\w+#.-]+)")
_NOT_A_LANG = {"default", "none", "text", "plain", "plaintext"}
# Lines longer than this skip the per-line regexes: page text is attacker-controlled and
# the lazy patterns go quadratic-to-cubic on long runs of backticks or brackets (5000
# backticks took 7.7 s, 100k brackets 55 s, holding the GIL). No real code line is this long.
_MAX_REGEX_LINE = 2000
_LINK = re.compile(r"!?\[([^\]\n]{0,500})\]\([^)\s]{0,2000}\)")
_IMG_IN_LINK = re.compile(r"\[!\[([^\]\n]{0,500})\]\([^)\s]{0,2000}\)\]\([^)\s]{0,2000}\)")
_FENCE = re.compile(r"^(`{3,})\n(.*?)\n\1$", re.M | re.S)
_ONE_LINE = re.compile(r"(`*) ?(.+?) ?\1")  # a line that is all one code span, or bare
_INLINE = re.compile(r"(`+)(.+?)\1")
_TABLE_SEP = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")
_PIPE = re.compile(r"(?<!\\)\|")


def _code_langs(html: str) -> dict[str, str]:
    """Every ``<pre>`` text -> the language named by a class on its <code>, itself or two
    wrappers up (Sphinx puts ``highlight-python`` on the grandparent), else ''."""
    try:
        tree = lxml.html.fromstring(html)
    except Exception:
        return {}
    langs: dict[str, str] = {}
    for pre in tree.iter("pre"):
        own = (_LANG.search(el.get("class") or "") for el in (*pre.iterchildren("code"), pre))
        up = (_WRAPPER_LANG.search(el.get("class") or "")
              for el in list(pre.iterancestors())[:2])
        m = next(filter(None, (*own, *up)), None)
        lang = m[1] if m and m[1].lower() not in _NOT_A_LANG else ""
        langs.setdefault(pre.text_content().strip(), lang)
    return langs


def _fix_markdown(md: str, html: str) -> str:
    """Restore what trafilatura's markdown drops: the code-fence language (it keeps the
    code but not its class), the fence of a one-line <pre> (it becomes an inline code
    span or a plain paragraph), and the separator row of a table without <th> (invalid
    markdown without it)."""
    langs = _code_langs(html) if "<pre" in html else {}
    if "```" in md and langs:
        md = _FENCE.sub(lambda m: f"{m[1]}{langs.get(m[2].strip(), '')}\n{m[2]}\n{m[1]}", md)
    lines = md.split("\n")
    out: list[str] = []
    fence = False
    for i, line in enumerate(lines):
        if (not fence and langs and len(line) <= _MAX_REGEX_LINE
                and (m := _ONE_LINE.fullmatch(line)) and m[2] in langs):
            out += [f"```{langs[m[2]]}", m[2], "```"]
            continue
        out.append(line)
        if line.startswith("```"):
            fence = not fence
        elif (not fence and line.startswith("|") and not (i and lines[i - 1].startswith("|"))
              and not _TABLE_SEP.match(lines[i + 1] if i + 1 < len(lines) else "")):
            out.append("|" + "---|" * (len(_PIPE.findall(line)) - 1))
    return "\n".join(out)


def _demark(md: str) -> str:
    """Markdown -> plain text: drop fences, table separators, heading marks, link/image
    syntax, bold/strike and code-span backticks, leaving code verbatim."""
    out: list[str] = []
    fence = False
    for line in md.split("\n"):
        if line.startswith("```"):
            fence = not fence
            continue
        if not fence and len(line) <= _MAX_REGEX_LINE:
            if _TABLE_SEP.match(line):
                continue
            parts = _INLINE.split(re.sub(r"^#{1,6} +", "", line))
            # split() yields [prose, ticks, code, prose, ...]: de-mark only the prose
            for i in range(0, len(parts), 3):
                # a linked badge [![alt](img)](href) first, or the outer link is left half-done
                p = _LINK.sub(r"\1", _IMG_IN_LINK.sub(r"\1", parts[i]))
                parts[i] = re.sub(r"(\*\*|~~)(.+?)\1", r"\2", p)
            line = "".join(p for i, p in enumerate(parts) if i % 3 != 1)
        out.append(line)
    return "\n".join(out).strip()


def _to_format(content_md: str, fmt: str, html_source: str) -> str:
    """content_md is markdown; convert to the requested output format."""
    if fmt == "text":
        return _demark(content_md)  # same extraction as markdown, so the formats agree
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

    ``clean`` (default) is trafilatura's balanced mode; ``clean=False`` favours recall.
    ``include_links`` keeps inline
    links. Returns ``{"content", "format", "title", "metadata"}`` where metadata is
    ``{"author", "published", "lang", "site", "word_count"}``. Whatever real text
    the tiers recover is returned verbatim - even a one-word page. ``content`` is
    ``""`` (``word_count`` 0) only when no tier extracted any text at all.
    """
    meta = _metadata(html, url)
    low = html.lower()
    if (".pdf" in low or "/download/" in low) and (pdfs := pdf_links(html, url)):
        meta["pdf_links"] = pdfs

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
                    favor_recall=not clean,
                )
                or ""
            ).strip()
        )

    # Precision keeps the page's markdown structure (links, paragraphs) but drops content
    # on non-article pages; balanced finds that content but can flatten a small page into
    # one paragraph. Keep precision unless balanced recovers clearly more text. WCXB
    # (2026-10-02, 50 pages/type): precision alone 0.684 F1, balanced alone 0.715.
    content = _tier1(clean)
    if clean:
        balanced = _tier1(False)
        if len(balanced.split()) > _RECALL_GAIN * len(content.split()):
            content = balanced

    # Tier 2: readability.
    if not content:
        content = _readability_markdown(html)

    # Tier 3: raw markdownify.
    if not content:
        content = _md(html, heading_style="ATX").strip()

    content = _dedup_blocks((content or "").strip())
    if content:
        content = _fix_markdown(content, html)

    # Convert to the requested format only when a tier actually recovered text;
    # otherwise the format converters can fabricate empty wrappers (e.g.
    # readability emits ``<body id="readabilityBody"></body>`` for empty input).
    if content and fmt != "markdown":
        content = _to_format(content, fmt, html) or ""

    word_count = len(content.split())

    title = meta.pop("title")
    meta["word_count"] = word_count

    return {"content": content, "format": fmt, "title": title, "metadata": meta}
