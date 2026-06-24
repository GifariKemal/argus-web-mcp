"""Tests for argus.extract.pdf - pymupdf4llm default path + lazy docling quality path."""

import fitz  # pymupdf
import pytest

from argus.extract.pdf import extract_pdf


def _make_pdf(pages_text: list[str]) -> bytes:
    """Generate a small digital (text-layer) PDF in memory."""
    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        page.insert_text((72, 100), text, fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def two_page_pdf() -> bytes:
    return _make_pdf(
        [
            "PAGEONE central banks hold rates steady today",
            "PAGETWO inflation expected to moderate next quarter",
        ]
    )


def test_all_pages(two_page_pdf):
    res = extract_pdf(two_page_pdf)
    assert res["pages_total"] == 2
    assert res["pages_returned"] == 2
    assert "PAGEONE" in res["content"]
    assert "PAGETWO" in res["content"]


def test_single_page_selection(two_page_pdf):
    res = extract_pdf(two_page_pdf, pages="1")
    assert res["pages_total"] == 2
    assert res["pages_returned"] == 1
    assert "PAGEONE" in res["content"]
    assert "PAGETWO" not in res["content"]


def test_page_range(two_page_pdf):
    res = extract_pdf(two_page_pdf, pages="1-2")
    assert res["pages_returned"] == 2
    assert "PAGEONE" in res["content"]
    assert "PAGETWO" in res["content"]


def test_second_page_only(two_page_pdf):
    res = extract_pdf(two_page_pdf, pages="2")
    assert res["pages_returned"] == 1
    assert "PAGETWO" in res["content"]
    assert "PAGEONE" not in res["content"]


def test_return_shape(two_page_pdf):
    res = extract_pdf(two_page_pdf)
    assert set(res.keys()) == {
        "pages_total",
        "pages_returned",
        "content",
        "tables",
        "metadata",
    }
    assert isinstance(res["tables"], list)
    assert isinstance(res["metadata"], dict)


def test_tables_mode_still_returns_content(two_page_pdf):
    res = extract_pdf(two_page_pdf, mode="tables")
    assert "PAGEONE" in res["content"]
    assert isinstance(res["tables"], list)


def test_not_a_pdf_raises():
    with pytest.raises(ValueError, match="not_pdf"):
        extract_pdf(b"not a pdf")


def test_empty_bytes_raises():
    with pytest.raises(ValueError, match="not_pdf"):
        extract_pdf(b"")


@pytest.mark.slow
def test_docling_quality_path(two_page_pdf):
    pytest.importorskip("docling")
    from argus.extract.pdf import extract_pdf_quality

    res = extract_pdf_quality(two_page_pdf)
    assert "PAGEONE" in res["content"]
    assert res["pages_total"] == 2
