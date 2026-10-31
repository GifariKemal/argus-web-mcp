"""Documents and feeds for ``read``: DOCX/PPTX/XLSX/EPUB/CSV/JSON/XML/RSS/Atom -> markdown.

``convert`` runs on the document worker pool (``convert_async``), so a hostile file burns
a document worker, never the event loop or the article workers. A zip is checked (entry
count, declared size, ratio) before any part is parsed, every part read is capped and so is
their sum, and every XML part goes through a parser that loads no DTD, resolves no entity
and touches no network. PDFs are not converted here: ``read`` runs ``extract.pdf`` on the
same pool and shapes the result with ``from_pdf``. Every cap on output (rows, columns,
sheets, items, pages, chars) is flagged (``truncated`` + counts), never silent.
"""

from __future__ import annotations

import csv
import functools
import html
import io
import itertools
import json
import posixpath
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import lxml.etree
import pymupdf
import pymupdf4llm

from .. import config
from . import ocr, pool
from .article import _demark
from .pdf import _FAST_TEXT_MIN_PAGES, _extract_plain_text

MAX_ENTRIES = 10_000
MAX_UNZIPPED = 200 * 1024 * 1024  # sum of the declared sizes
MAX_ENTRY_BYTES = 32 * 1024 * 1024  # per part actually read, whatever its header claims
MAX_READ_BYTES = 64 * 1024 * 1024  # all parts one conversion reads, together
# Real XML parts deflate 10-30:1; a bomb runs near deflate's ~1000:1 ceiling.
MAX_RATIO = 200
_RATIO_MIN_BYTES = 1024 * 1024  # small parts compress however they like
MAX_ROWS = 2_000  # per CSV / sheet
MAX_ITEMS = 200  # per feed
MAX_SHEETS = 50
MAX_COLS = 200  # per table: one 20k-field row must not pad every other row to 20k
MAX_CHARS = 5_000_000  # converted content, the last line of defence for every kind
# Pretty-printing re-indents every level: 10 KB nested 100 deep came out at 1 MB.
_JSON_PRETTY_MAX_DEPTH = 16
_JSON_PRETTY_MAX_BYTES = 1024 * 1024

_CTYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/epub+zip": "epub",
    "text/csv": "csv",
    "application/csv": "csv",
    "application/json": "json",
    "application/rss+xml": "feed",
    "application/atom+xml": "feed",
    "application/rdf+xml": "feed",
    "text/xml": "xml",
    "application/xml": "xml",
    **dict.fromkeys(("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
                     "image/tiff", "image/bmp"), "image"),  # not svg: that is XML text
}
_EXTS = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx", ".epub": "epub",
         ".csv": "csv", ".json": "json", ".xml": "xml", ".rss": "feed", ".atom": "feed",
         **dict.fromkeys((".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"),
                         "image")}
BINARY_KINDS = frozenset({"pdf", "docx", "pptx", "xlsx", "epub", "zip", "image"})
_ZIP_KINDS = BINARY_KINDS - {"pdf", "image"}
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\0",
                b"MM\0*")
# "BM" alone matched any text starting with it: a BMP also has zeroed reserved bytes and
# one of the known DIB header sizes.
_BMP_DIB_SIZES = frozenset({12, 16, 40, 52, 56, 64, 108, 124})
_FEED_ROOTS = frozenset({"rss", "feed", "RDF"})
_ROOT = re.compile(r"<([A-Za-z_][\w.:-]*)")  # first element; skips <?xml and <!DOCTYPE
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")  # lxml refuses str input that declares one
_TAG = re.compile(r"<[^<>]*>")  # linear: never scans past the next '<'
_COUNTS = (("rows_total", "rows_returned"), ("items_total", "items_returned"),
           ("pages_total", "pages_returned"), ("cols_total", "cols_returned"),
           ("sheets_total", "sheets_returned"), ("chars_total", "chars_returned"),
           ("frames_total", "frames_returned"), ("ocr_pages_total", "ocr_pages_returned"),
           ("charts_total", "charts_returned"))

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_HEADING = re.compile(r"Heading([1-6])")
_ROW_OPEN = re.compile(rb"<(?:[\w.-]+:)?row[\s/>]")  # <row ...>, <x:row/>; not <rowBreaks>
_SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml")
_SAFE_XML = {"resolve_entities": False, "no_network": True, "huge_tree": False,
             "load_dtd": False}  # no DTD, no entities (XXE, billion laughs), no network
_LINK_UNSAFE = re.compile(r"[()<>\s]")  # would end or break out of a markdown link target
_COL = re.compile(r"[A-Z]{1,3}(?=\d)")  # XFD is the last column: longer refs are invalid


class DocError(ValueError):
    """A document refused by a guard or of no supported kind. ``reason`` and ``detail`` are
    our own words (never text from the file), so they can go to the client."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(reason, detail)  # args round-trip: raised in a worker, pickled back
        self.reason, self.detail = reason, detail


def sniff(content_type: str, body: bytes | None, text: str, url: str) -> str | None:
    """Document kind of a fetched body, or None for a page (the article path). The served
    type decides first, then magic bytes, then the URL extension; XML is a feed when its root
    is rss/feed/rdf:RDF. ``body`` is the raw bytes ``fetch_static`` keeps for non-text types."""
    if "html" in content_type:
        return None
    kind = _CTYPES.get(content_type) or (
        "json" if content_type.endswith("+json")
        else "xml" if content_type.endswith("+xml") else None)
    if kind is None and body:
        kind = ("zip" if body.startswith(b"PK\x03\x04")
                else "pdf" if b"%PDF-" in body[:1024]  # PDF may sit behind junk
                else "image" if body.startswith(_IMAGE_MAGIC) or _is_bmp(body)
                or (body[:4] == b"RIFF" and body[8:12] == b"WEBP") else None)
    if kind is None:
        kind = _EXTS.get(PurePosixPath(urlsplit(url).path).suffix.lower())
        if kind in BINARY_KINDS and body is None:  # text at a .docx URL is not a docx
            return None
    if kind in ("xml", "feed"):
        m = _ROOT.search(_COMMENT.sub("", text[:4096]))
        root = m[1].rpartition(":")[2] if m else ""
        return None if root == "html" else "feed" if root in _FEED_ROOTS else "xml"
    return kind


def _is_bmp(body: bytes) -> bool:
    return (body[:2] == b"BM" and body[6:10] == bytes(4)
            and int.from_bytes(body[14:18], "little") in _BMP_DIB_SIZES)


async def convert_async(kind: str, data: bytes | str, url: str, fmt: str) -> dict:
    """``convert`` on the document pool (``pool.run_document``): TimeoutError past its
    budget, the overrunning worker killed, article extraction untouched."""
    return await pool.run_document(convert, kind, data, url, fmt)


def convert(kind: str, data: bytes | str, url: str, fmt: str = "markdown") -> dict:
    """``data`` (bytes for zip kinds, decoded text otherwise) -> ``{title, content, metadata,
    truncated}`` in ``fmt``. A zip's own parts decide docx/pptx/xlsx/epub, whatever it was
    served as. Raises DocError on a guard; parser errors propagate."""
    meta: dict = {}
    if kind in _ZIP_KINDS:
        zf = _open_zip(data)
        kind = _zip_kind(zf)
        src = data if kind == "epub" else zf
    else:
        src = data
    title, content = _CONVERTERS[kind](src, meta)
    return _finish(kind, title, content, meta, fmt)


def from_pdf(result: dict, fmt: str) -> dict:
    """``extract_pdf``'s result in ``convert``'s shape; a page cap shows as truncated."""
    meta = dict(result["metadata"])
    meta.pop("pages_capped", None)  # pages_total != pages_returned says it
    title = meta.pop("title", None) or None
    meta.update(pages_total=result["pages_total"], pages_returned=result["pages_returned"])
    return _finish("pdf", title, result["content"], meta, fmt)


def _finish(kind: str, title: str | None, content: str, meta: dict, fmt: str) -> dict:
    meta["doc_type"] = kind
    if len(content) > MAX_CHARS:
        meta.update(chars_total=len(content), chars_returned=MAX_CHARS)
        content = content[:MAX_CHARS]
    meta.setdefault("word_count", len(content.split()))
    truncated = any(meta.get(total) != meta.get(kept) for total, kept in _COUNTS)
    if fmt == "text":
        content = _demark(content)
    elif fmt == "html":
        meta["format_note"] = "documents are returned as markdown for format='html'"
    return {"title": title, "content": content, "metadata": meta, "truncated": truncated}


# --- zip guard + safe XML ----------------------------------------------------------------


def _open_zip(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(io.BytesIO(data))
    zf.read_budget = MAX_READ_BYTES  # spent by _read: parts re-read or aliased add up
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise DocError("doc_too_large", f"{len(infos)} zip entries (max {MAX_ENTRIES})")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNZIPPED:
        raise DocError("doc_too_large", f"{total} bytes uncompressed (max {MAX_UNZIPPED})")
    for i in infos:
        if i.file_size > _RATIO_MIN_BYTES and i.file_size > MAX_RATIO * max(i.compress_size, 1):
            raise DocError("doc_too_large", f"compression ratio over {MAX_RATIO}:1")
    return zf


def _read(zf: zipfile.ZipFile, name: str) -> bytes:
    with zf.open(name) as f:  # a lying header cannot overrun either cap
        data = f.read(min(MAX_ENTRY_BYTES, zf.read_budget) + 1)
    if len(data) > MAX_ENTRY_BYTES:
        raise DocError("doc_too_large", f"a part is over {MAX_ENTRY_BYTES} bytes")
    zf.read_budget -= len(data)
    if zf.read_budget < 0:
        raise DocError("doc_too_large", f"parts read add up to over {MAX_READ_BYTES} bytes")
    return data


def _zip_kind(zf: zipfile.ZipFile) -> str:
    names = set(zf.namelist())
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if any(_SLIDE.fullmatch(n) for n in names):
        return "pptx"
    if "mimetype" in names and _read(zf, "mimetype").strip() == b"application/epub+zip":
        return "epub"
    raise DocError("unsupported_document", "zip is not a docx, pptx, xlsx or epub")


def _xml(data: bytes | str, recover: bool = False):
    """Parse untrusted XML: no DTD, no entities (XXE, billion laughs), no network."""
    parser = lxml.etree.XMLParser(recover=recover, **_SAFE_XML)
    if isinstance(data, str):
        data = _XML_DECL.sub("", data.lstrip("\ufeff"), count=1)
    # ponytail: whole-tree parse, ~10x the part in RAM (32 MB cap); worksheets, the part
    # that is mostly rows, stream instead (_xlsx_rows). Stream others if workers OOM.
    root = lxml.etree.fromstring(data, parser)
    if root is None:  # recover mode on a body with no element
        raise DocError("unsupported_document", "no XML root element")
    return root


def _local(el) -> str:
    return el.tag.rpartition("}")[2] if isinstance(el.tag, str) else ""  # comments, entities


def _kid(el, *names):
    """First child with one of these local names, in the order the names are given."""
    kids: dict = {}
    for c in el:
        kids.setdefault(_local(c), c)
    return next((kids[n] for n in names if n in kids), None)


def _plain(s: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", s)).split())


def _table(rows: list[list[str]], meta: dict) -> str:
    width = _note_cols(meta, max(map(len, rows)))
    cells = [[str(c).replace("|", "\\|").replace("\n", " ").strip() for c in r[:width]]
             + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(r) + " |" for r in cells]
    return "\n".join([lines[0], "|" + "---|" * width, *lines[1:]])


def _note_cols(meta: dict, total: int) -> int:
    """Columns a table keeps (``MAX_COLS``); the widest table's counts go to ``meta``."""
    kept = min(total, MAX_COLS)
    meta["cols_total"] = max(meta.get("cols_total", 0), total)
    meta["cols_returned"] = max(meta.get("cols_returned", 0), kept)
    return kept


def _core_title(zf: zipfile.ZipFile) -> str | None:
    if "docProps/core.xml" not in zf.namelist():
        return None
    t = _kid(_xml(_read(zf, "docProps/core.xml")), "title")
    return _plain(t.text or "") or None if t is not None else None


# --- converters: (src, meta) -> (title, content) ----------------------------------------


def _docx(zf: zipfile.ZipFile, meta: dict) -> tuple[str | None, str]:
    body = _xml(_read(zf, "word/document.xml")).find(_W + "body")
    blocks = []
    for el in _docx_blocks(body if body is not None else []):
        if el.tag == _W + "tbl":
            trs = el.findall(_W + "tr")
            tcs = [tr.findall(_W + "tc") for tr in trs[:MAX_ROWS]]
            # Counts summed over every table: totals differ from returned iff one was cut.
            meta["rows_total"] = meta.get("rows_total", 0) + len(trs)
            meta["rows_returned"] = meta.get("rows_returned", 0) + len(tcs)
            width = _note_cols(meta, max(map(len, tcs), default=0))  # text only for kept cells
            rows = [[" ".join(filter(None, map(_docx_text, tc.iter(_W + "p"))))
                     for tc in r[:width]] for r in tcs]
            blocks.append(_table(rows, meta) if any(rows) else "")
        else:
            blocks.append(_docx_para(el))
    return _core_title(zf), "\n\n".join(b for b in blocks if b)


def _docx_blocks(el):
    """Body-level paragraphs and tables, also inside content controls and tracked inserts."""
    for c in el:
        if c.tag in (_W + "p", _W + "tbl"):
            yield c
        elif c.tag != _W + "sectPr":
            yield from _docx_blocks(c)


def _docx_text(p) -> str:
    return "".join((el.text or "") if el.tag == _W + "t" else "\t" if el.tag == _W + "tab"
                   else "\n" for el in p.iter(_W + "t", _W + "tab", _W + "br")).strip()


def _docx_para(p) -> str:
    text = _docx_text(p)
    if not text:
        return ""
    style = p.find(f"{_W}pPr/{_W}pStyle")
    sid = style.get(_W + "val", "") if style is not None else ""
    if sid == "Title":
        return "# " + text
    if m := _HEADING.fullmatch(sid):
        return "#" * int(m[1]) + " " + text
    if p.find(f"{_W}pPr/{_W}numPr") is not None:
        return "- " + text
    return text


def _pptx(zf: zipfile.ZipFile, meta: dict) -> tuple[str | None, str]:
    slides = sorted((int(m[1]), n) for n in zf.namelist() if (m := _SLIDE.fullmatch(n)))
    out, charts = [], {}  # charts: part path -> the slide that printed it
    for num, name in slides:
        # Text, picture alt text and charts in slide order: authors put captions and facts
        # in `descr`, and a chart's numbers exist nowhere else in the file.
        parts, rels = [], None
        for el in _xml(_read(zf, name)).iter(_A + "p", _P + "cNvPr", _C + "chart"):
            if el.tag == _A + "p":
                parts.append("".join(t.text or "" for t in el.iter(_A + "t")).strip())
            elif el.tag == _C + "chart":
                rels = _part_rels(zf, name) if rels is None else rels
                path = rels.get(el.get(_R_ID))
                # Counted, so a chart whose rel or part is missing is flagged, not dropped.
                meta["charts_total"] = meta.get("charts_total", 0) + 1
                meta.setdefault("charts_returned", 0)
                if path in charts:  # one chart on many slides: parsed and printed once
                    parts.append(f"(same chart as slide {charts[path]})")
                elif path is not None and path in zf.NameToInfo:
                    charts[path] = num
                    parts.append(_chart(_xml(_read(zf, path)), meta))
                else:
                    continue
                meta["charts_returned"] += 1
            elif el.getparent().tag == _P + "nvPicPr" and (alt := (el.get("descr") or "").strip()):
                parts.append("![" + " ".join(alt.split()).replace("]", "\\]") + "](image)")
        out.append("\n\n".join([f"## Slide {num}", *filter(None, parts)]))
    return _core_title(zf), "\n\n".join(out)


def _part_rels(zf: zipfile.ZipFile, name: str) -> dict[str, str]:
    """A part's relationship ids -> zip paths (``a/b.xml`` keeps ``a/_rels/b.xml.rels``)."""
    folder, base = posixpath.split(name)
    rels = f"{folder}/_rels/{base}.rels"
    if rels not in zf.NameToInfo:
        return {}
    return {r.get("Id"): posixpath.normpath(posixpath.join(folder, t)) if not t.startswith("/")
            else t.lstrip("/") for r in _xml(_read(zf, rels)) if (t := r.get("Target", ""))}


def _chart(root, meta: dict) -> str:
    """A chart part: its title, then categories x series as a table (row/column caps flagged
    like any table). Scatter charts keep their points in xVal/yVal."""
    title = root.find(f"{_C}chart/{_C}title")
    title = " ".join("".join(t.text or "" for t in title.iter(_A + "t")).split()) \
        if title is not None else ""
    cats: dict[int, str] = {}
    series = []
    for n, ser in enumerate(root.iter(_C + "ser"), 1):
        name = "".join(v.text or "" for v in ser.iterfind(f"{_C}tx//{_C}v")).strip()
        series.append((name or f"Series {n}", _chart_points(ser, "val", "yVal")))
        cats = cats or _chart_points(ser, "cat", "xVal")
    head = f"### Chart: {title}" if title else "### Chart"
    if not series:
        return head
    series = series[:_note_cols(meta, len(series) + 1) - 1]
    idxs = sorted(set(cats).union(*(pts for _, pts in series)))
    meta["rows_total"] = meta.get("rows_total", 0) + len(idxs)
    meta["rows_returned"] = meta.get("rows_returned", 0) + min(len(idxs), MAX_ROWS)
    rows = [["Category", *(name for name, _ in series)]] + [
        [cats.get(i, ""), *(pts.get(i, "") for _, pts in series)] for i in idxs[:MAX_ROWS]]
    return f"{head}\n\n{_table(rows, meta)}"


def _chart_points(ser, *kinds: str) -> dict[int, str]:
    """``{idx: value}`` from the first of ``kinds`` the series has (str/num ref or literal)."""
    el = next((e for k in kinds if (e := ser.find(_C + k)) is not None), None)
    if el is None:
        return {}
    return {int(i): pt.findtext(_C + "v") or "" for pt in el.iter(_C + "pt")
            if (i := pt.get("idx", "")).isdigit() and len(i) < 10}  # no 10k-digit int()


def _xlsx(zf: zipfile.ZipFile, meta: dict) -> tuple[str | None, str]:
    names = set(zf.namelist())
    strings = []
    if "xl/sharedStrings.xml" in names:  # phonetic runs (rPh) are not the cell's text
        strings = ["".join(t.text or "" for t in si.iter(_S + "t")
                           if t.getparent().tag != _S + "rPh")
                   for si in _xml(_read(zf, "xl/sharedStrings.xml")).iter(_S + "si")]
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels = {r.get("Id"): r.get("Target", "")
                for r in _xml(_read(zf, "xl/_rels/workbook.xml.rels"))}
    sheets = list(_xml(_read(zf, "xl/workbook.xml")).iter(_S + "sheet"))
    meta.update(sheets_total=len(sheets), sheets_returned=min(len(sheets), MAX_SHEETS))
    out, seen, meta["rows_total"], meta["rows_returned"] = [], {}, {}, {}
    for i, sheet in enumerate(sheets[:MAX_SHEETS], 1):
        name = sheet.get("name") or f"Sheet{i}"
        target = rels.get(sheet.get(_R_ID)) or f"worksheets/sheet{i}.xml"
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if path in seen:  # many sheets aliasing one part: parsed and printed once
            out.append(f"## {name}\n\n(same worksheet as {seen[path]})")
            continue
        seen[path] = name
        kept, total = _xlsx_rows(_read(zf, path), strings)
        # Only columns holding a value, at most MAX_COLS of them: sparse cells spread over
        # 16k columns must not widen every row.
        cols = sorted({k for r in kept for k, v in r.items() if v})
        cols = cols[:_note_cols(meta, len(cols))]
        meta["rows_total"][name], meta["rows_returned"][name] = total, len(kept)
        table = _table([[r.get(k, "") for k in cols] for r in kept], meta) if cols else ""
        out.append(f"## {name}\n\n{table}".strip())
    return _core_title(zf), "\n\n".join(out)


def _xlsx_rows(data: bytes, strings: list[str]) -> tuple[list[dict], int]:
    """Stream a worksheet: the first MAX_ROWS rows as ``{col: text}``, the rest only counted.
    A whole tree of 31 MB of ``<row/>`` peaked near 1 GB per worker; this stops parsing at
    the cap. Walking the rest in Python cost ~3 us a row (1.9M empty rows: 5.6 s), so past
    the cap they are counted on the bytes, at C speed and no extra memory."""
    kept = []
    for _, row in lxml.etree.iterparse(io.BytesIO(data), tag=_S + "row", **_SAFE_XML):
        kept.append({_xlsx_col(c.get("r", ""), n): _xlsx_cell(c, strings)
                     for n, c in enumerate(row.iter(_S + "c"))})
        row.clear(keep_tail=True)
        if len(kept) == MAX_ROWS:
            break
    else:
        return kept, len(kept)
    # ponytail: a byte count, so a `<row` inside an XML comment counts as a row too (an
    # over-count only sets `truncated`). Any prefix counts: a file mixing two prefixes bound
    # to the same namespace must not hide rows past the cap.
    return kept, len(_ROW_OPEN.findall(data))


def _xlsx_col(ref: str, default: int) -> int:
    m = _COL.match(ref)
    return functools.reduce(lambda n, ch: n * 26 + ord(ch) - 64, m[0], 0) - 1 if m else default


def _xlsx_cell(c, strings: list[str]) -> str:
    t = c.get("t")
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(_S + "t"))
    v = c.findtext(_S + "v") or ""  # numbers as written, not reformatted
    if t == "s":
        return strings[int(v)] if v.isdigit() and int(v) < len(strings) else ""
    return v


def _epub(data: bytes, meta: dict) -> tuple[str | None, str]:
    with pymupdf.open(stream=data, filetype="epub") as doc:
        info = doc.metadata or {}
        title = info.get("title") or None
        if info.get("author"):
            meta["author"] = info["author"]
        pages = list(range(min(doc.page_count, config.PDF_MAX_PAGES)))  # extract_pdf's cap
        meta.update(pages_total=doc.page_count, pages_returned=len(pages))
        content = ""
        if len(pages) < _FAST_TEXT_MIN_PAGES:  # same budget rule as extract_pdf
            try:
                chunks = pymupdf4llm.to_markdown(doc, pages=pages, page_chunks=True,
                                                 show_progress=False, use_ocr=False)
                content = "\n\n".join(c["text"].strip() for c in chunks).strip()
            except Exception:  # noqa: BLE001 - layout engine on a reflowable doc: page text
                content = ""
        meta["engine"] = "pymupdf4llm" if content else "pymupdf-text"
        if not content:
            content, _ = _extract_plain_text(doc, pages)
    return title, content


def _image(data: bytes, meta: dict) -> tuple[None, str]:
    """OCR text of an image URL (first frame, grayscale), never of a page's media list."""
    if ocr.tesseract() is None:  # local dev and CI have no binary: say so, do not crash
        raise DocError("unsupported_document", "ocr unavailable")
    try:
        png = ocr.image_png(data, meta)
    except ocr.Refused as exc:  # our own words: a size, or the format allowlist
        raise DocError(exc.reason, exc.detail) from None
    meta["ocr"] = ocr.OCR_META
    return None, ocr.ocr_png(png, ocr.OCR_TIMEOUT_S)


def _csv(text: str, meta: dict) -> tuple[None, str]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:  # one column, or nothing to go on
        dialect = csv.excel
    rows = (r for r in csv.reader(io.StringIO(text), dialect) if r)
    kept = list(itertools.islice(rows, MAX_ROWS))
    meta["rows_total"] = len(kept) + sum(1 for _ in rows)
    meta["rows_returned"] = len(kept)
    return None, _table(kept, meta) if kept else ""


def _json(text: str, meta: dict) -> tuple[None, str]:
    """Pretty-printed when small and shallow, else the text as served. Parsed either way, so
    an invalid body still fails here and ``read`` falls back to the page path."""
    data = json.loads(text)
    if len(text) <= _JSON_PRETTY_MAX_BYTES and _json_depth(data) <= _JSON_PRETTY_MAX_DEPTH:
        text = json.dumps(data, indent=2, ensure_ascii=False)
    return None, f"```json\n{text.strip()}\n```"


def _json_depth(data) -> int:
    """Container nesting depth, iteratively (no recursion), stopping past the pretty limit."""
    peak, stack = 0, [(data, 1)]
    while stack and peak <= _JSON_PRETTY_MAX_DEPTH:
        node, depth = stack.pop()
        if isinstance(node, dict | list):
            peak = max(peak, depth)
            stack.extend((v, depth + 1)
                         for v in (node.values() if isinstance(node, dict) else node))
    return peak


def _xml_fence(text: str, meta: dict) -> tuple[None, str]:
    return None, f"```xml\n{text.strip()}\n```"  # never parsed: only feeds are


def _feed(text: str, meta: dict) -> tuple[str | None, str]:
    """RSS 2.0 / RSS 1.0 (RDF) / Atom by local names, so every namespace variant reads."""
    root = _xml(text, recover=True)  # feeds in the wild carry stray HTML entities
    chan = _kid(root, "channel")
    title = _text(_kid(chan if chan is not None else root, "title")) or None
    holder = chan if _local(root) == "rss" and chan is not None else root
    entries = [c for c in holder if _local(c) in ("item", "entry")]
    items, lines = [], [f"# {title}", ""] if title else []
    for e in entries[:MAX_ITEMS]:
        url = _link(e)  # only web links: a javascript:/data: href is no link to follow
        it = {"title": _text(_kid(e, "title")),
              "url": url if urlsplit(url).scheme.lower() in ("http", "https") else "",
              "published": _text(_kid(e, "pubDate", "published", "updated", "date")),
              "summary": _text(_kid(e, "description", "summary", "content"))}
        author = _kid(e, "creator", "author", "source")
        name = _kid(author, "name") if author is not None else None
        head = _md_link(it["title"] or it["url"], it["url"]) if it["url"] else it["title"]
        by = _text(name if name is not None else author)
        line = "- " + " - ".join(p for p in (head, it["published"], by) if p)
        lines.append(line + (f"\n  {it['summary']}" if it["summary"] else ""))
        items.append(it)
    meta.update(items=items, items_total=len(entries), items_returned=len(items))
    return title, "\n".join(lines)


def _md_link(label: str, url: str) -> str:
    """A markdown link neither part can break out of: ``](``, ``)`` or a space in a feed's
    link used to start a second, injected link."""
    label = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({_LINK_UNSAFE.sub(lambda m: quote(m[0], safe=''), url)})"


def _text(el) -> str:
    return _plain("".join(el.itertext())) if el is not None else ""


def _link(e) -> str:
    for c in e:
        if _local(c) == "link":
            if c.get("href") and c.get("rel", "alternate") == "alternate":
                return c.get("href").strip()
            if (c.text or "").strip():
                return c.text.strip()
    return ""


_CONVERTERS = {"docx": _docx, "pptx": _pptx, "xlsx": _xlsx, "epub": _epub, "csv": _csv,
               "json": _json, "xml": _xml_fence, "feed": _feed, "image": _image}
