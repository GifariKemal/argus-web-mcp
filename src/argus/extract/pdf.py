"""PDF extraction. Default path: pymupdf4llm -> markdown. Quality path: docling (lazy)."""

from __future__ import annotations

from typing import Any

import fitz  # pymupdf
import pymupdf4llm

_FAST_TEXT_MIN_PAGES = 20


def _parse_pages(pages: str | None, total: int) -> list[int]:
    """Parse '1-5' / '3' (1-indexed inclusive) into a 0-indexed page list.

    None -> all pages. Reversed bounds are swapped; partially out-of-range bounds are
    clamped to the document. A malformed spec ('abc', '5-') or a range entirely
    outside the document raises ``ValueError('bad_pages')`` - distinct from the
    ``'not_pdf'`` ValueError so callers can return an accurate error code instead of
    mislabeling a valid PDF.
    """
    if pages is None:
        return list(range(total))
    spec = pages.strip()
    try:
        if "-" in spec:
            lo_s, hi_s = spec.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(spec)
    except ValueError:
        raise ValueError("bad_pages") from None
    if lo > hi:
        lo, hi = hi, lo
    lo = max(1, lo)
    hi = min(total, hi)
    indices = [i - 1 for i in range(lo, hi + 1)]
    if not indices:
        raise ValueError("bad_pages")
    return indices


def _open(data: bytes) -> fitz.Document:
    if not data:
        raise ValueError("not_pdf")
    # pymupdf will happily render HTML/XPS/images as a doc; require the PDF magic so a
    # non-PDF served at a .pdf URL is rejected instead of silently "extracted". Per the
    # PDF spec (ISO 32000 implementation note), the header may be preceded by up to
    # 1024 bytes of junk - real-world PDFs behind naive proxies/CGI do this and pymupdf
    # parses them fine, so search the first 1KiB instead of only byte 0.
    if b"%PDF-" not in data[:1024]:
        raise ValueError("not_pdf")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # pymupdf raises FileDataError / RuntimeError
        raise ValueError("not_pdf") from exc
    if doc.page_count == 0:
        doc.close()
        raise ValueError("not_pdf")
    return doc


def _find_tables(doc: fitz.Document, page_indices: list[int]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for idx in page_indices:
        found = doc[idx].find_tables()
        for t in found.tables:
            tables.append({"page": idx + 1, "rows": t.extract()})
    return tables


def _extract_plain_text(doc: fitz.Document, page_indices: list[int]) -> str:
    """Fast full-document text path for large PDFs.

    pymupdf4llm's markdown pipeline can spend tens of seconds on graphics-heavy
    report PDFs even when the caller only asked for text. This path still returns
    every requested page, but skips expensive layout/table inference.
    """
    parts: list[str] = []
    for idx in page_indices:
        text = doc[idx].get_text("text", sort=True).strip()
        if text:
            parts.append(f"## Page {idx + 1}\n\n{text}")
    return "\n\n".join(parts).strip()


def extract_pdf(data: bytes, pages: str | None = None, mode: str = "text") -> dict[str, Any]:
    """Extract a PDF to markdown using pymupdf4llm.

    pages: '1-5' or '3' (1-indexed inclusive) or None for all.
    mode='tables' still returns ``content`` but also populates ``tables``.
    Raises ``ValueError('not_pdf')`` on invalid input.
    """
    doc = _open(data)
    try:
        total = doc.page_count
        page_indices = _parse_pages(pages, total)
        metadata = dict(doc.metadata or {})

        if mode == "text" and len(page_indices) >= _FAST_TEXT_MIN_PAGES:
            metadata["engine"] = "pymupdf-fast-text"
            return {
                "pages_total": total,
                "pages_returned": len(page_indices),
                "content": _extract_plain_text(doc, page_indices),
                "tables": [],
                "metadata": metadata,
            }

        text_only = mode != "tables"
        chunks = pymupdf4llm.to_markdown(
            doc,
            pages=page_indices,
            page_chunks=True,
            table_strategy="lines_strict" if mode == "tables" else None,
            ignore_graphics=text_only,
            show_progress=False,
        )
        content = "\n\n".join(c["text"].strip() for c in chunks).strip()

        tables = _find_tables(doc, page_indices) if mode == "tables" else []

        return {
            "pages_total": total,
            "pages_returned": len(page_indices),
            "content": content,
            "tables": tables,
            "metadata": metadata,
        }
    finally:
        doc.close()


def _slice_pdf(data: bytes, pages: str | None) -> tuple[bytes, int, list[int]]:
    """Validate ``data`` and reduce it to the requested pages via pymupdf.

    Returns ``(pdf_bytes, total_pages, page_indices)`` where ``pdf_bytes`` contains
    ONLY the requested pages (the whole document when ``pages`` is None/full-range).
    Pure pymupdf - lets the heavy Docling tier honor the ``pages`` parameter instead
    of OCR-ing the entire document while claiming it sliced.
    """
    doc = _open(data)
    try:
        total = doc.page_count
        page_indices = _parse_pages(pages, total)
        if len(page_indices) != total:
            doc.select(page_indices)
            data = doc.tobytes()
    finally:
        doc.close()
    return data, total, page_indices


def extract_pdf_quality(data: bytes, pages: str | None = None) -> dict[str, Any]:
    """High-quality / scanned-document path via Docling (lazy import, optional dep).

    Docling runs OCR + layout models; far heavier than the default pymupdf4llm path.
    Install with the ``pdf-quality`` extra. Page totals are still sourced from pymupdf;
    ``pages`` is honored by slicing the PDF before it reaches Docling.
    """
    from docling.document_converter import DocumentConverter  # lazy, optional

    data, total, page_indices = _slice_pdf(data, pages)

    import io

    from docling.datamodel.base_models import DocumentStream

    source = DocumentStream(name="doc.pdf", stream=io.BytesIO(data))
    result = DocumentConverter().convert(source)
    content = result.document.export_to_markdown()

    return {
        "pages_total": total,
        "pages_returned": len(page_indices),
        "content": content,
        "tables": [],
        "metadata": {"engine": "docling"},
    }
