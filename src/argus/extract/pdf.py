"""PDF extraction. Default path: pymupdf4llm -> markdown. Quality path: docling (lazy)."""

from __future__ import annotations

from typing import Any

import fitz  # pymupdf
import pymupdf4llm


def _parse_pages(pages: str | None, total: int) -> list[int]:
    """Parse '1-5' / '3' (1-indexed inclusive) into a 0-indexed page list.

    None -> all pages. Out-of-range bounds are clamped to the document.
    """
    if pages is None:
        return list(range(total))
    spec = pages.strip()
    if "-" in spec:
        lo_s, hi_s = spec.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
    else:
        lo = hi = int(spec)
    lo = max(1, lo)
    hi = min(total, hi)
    return [i - 1 for i in range(lo, hi + 1)]


def _open(data: bytes) -> fitz.Document:
    if not data:
        raise ValueError("not_pdf")
    # pymupdf will happily render HTML/XPS/images as a doc; require the PDF magic so a
    # non-PDF served at a .pdf URL is rejected instead of silently "extracted".
    if data.lstrip()[:5] != b"%PDF-":
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

        table_strategy = "lines_strict" if mode == "tables" else "lines"
        chunks = pymupdf4llm.to_markdown(
            doc,
            pages=page_indices,
            page_chunks=True,
            table_strategy=table_strategy,
            show_progress=False,
        )
        content = "\n\n".join(c["text"].strip() for c in chunks).strip()
        metadata = dict(doc.metadata or {})

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


def extract_pdf_quality(data: bytes, pages: str | None = None) -> dict[str, Any]:
    """High-quality / scanned-document path via Docling (lazy import, optional dep).

    Docling runs OCR + layout models; far heavier than the default pymupdf4llm path.
    Install with the ``pdf-quality`` extra. Page totals are still sourced from pymupdf.
    """
    from docling.document_converter import DocumentConverter  # lazy, optional

    # Validate + get the page total cheaply via pymupdf.
    doc = _open(data)
    total = doc.page_count
    page_indices = _parse_pages(pages, total)
    doc.close()

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
