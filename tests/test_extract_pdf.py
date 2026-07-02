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


def _make_ruled_table_pdf() -> bytes:
    """A single-page PDF with a 2x2 ruled grid so find_tables() detects a real table."""
    doc = fitz.open()
    page = doc.new_page()
    x0, y0, x1, y1 = 72, 72, 272, 172
    page.draw_rect(fitz.Rect(x0, y0, x1, y1))
    page.draw_line(fitz.Point((x0 + x1) / 2, y0), fitz.Point((x0 + x1) / 2, y1))
    page.draw_line(fitz.Point(x0, (y0 + y1) / 2), fitz.Point(x1, (y0 + y1) / 2))
    page.insert_text((80, 95), "Name")
    page.insert_text((180, 95), "Score")
    page.insert_text((80, 145), "Gold")
    page.insert_text((180, 145), "42")
    data = doc.tobytes()
    doc.close()
    return data


def test_tables_mode_extracts_table_rows():
    # Moat feature: mode='tables' must populate tables[].rows with the cell grid.
    res = extract_pdf(_make_ruled_table_pdf(), mode="tables")
    assert len(res["tables"]) >= 1
    table = res["tables"][0]
    assert table["page"] == 1
    assert table["rows"] == [["Name", "Score"], ["Gold", "42"]]


def test_text_mode_does_not_populate_tables():
    # Default mode must skip the find_tables() pass entirely.
    res = extract_pdf(_make_ruled_table_pdf(), mode="text")
    assert res["tables"] == []


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


# --- pages-spec validation (bad_pages, distinct from not_pdf) -------------------


def test_malformed_pages_spec_raises_bad_pages(two_page_pdf):
    for spec in ("abc", "5-", "-3", "1-x"):
        with pytest.raises(ValueError, match="bad_pages"):
            extract_pdf(two_page_pdf, pages=spec)


def test_fully_out_of_range_pages_raises_bad_pages(two_page_pdf):
    with pytest.raises(ValueError, match="bad_pages"):
        extract_pdf(two_page_pdf, pages="99")


def test_reversed_range_is_swapped(two_page_pdf):
    res = extract_pdf(two_page_pdf, pages="2-1")
    assert res["pages_returned"] == 2


def test_pdf_with_leading_junk_bytes_parses(two_page_pdf):
    """Per the PDF spec the header may be preceded by up to 1024 junk bytes; pymupdf
    parses these fine and the magic gate must not reject them."""
    junky = b"HTTP-junk-prefix\r\n" + two_page_pdf
    res = extract_pdf(junky)
    assert res["pages_total"] == 2
    assert "PAGEONE" in res["content"]


def test_junk_beyond_1kib_still_rejected():
    with pytest.raises(ValueError, match="not_pdf"):
        extract_pdf(b"x" * 2048 + b"%PDF-1.4 fake")


# --- quality tier honors `pages` via _slice_pdf (pure pymupdf, no docling) ------


def test_slice_pdf_subsets_pages(two_page_pdf):
    from argus.extract.pdf import _slice_pdf

    data, total, page_indices = _slice_pdf(two_page_pdf, "2")
    assert total == 2
    assert page_indices == [1]
    sliced = extract_pdf(data)
    assert sliced["pages_total"] == 1
    assert "PAGETWO" in sliced["content"]
    assert "PAGEONE" not in sliced["content"]


def test_slice_pdf_full_range_returns_original(two_page_pdf):
    from argus.extract.pdf import _slice_pdf

    data, total, page_indices = _slice_pdf(two_page_pdf, None)
    assert data == two_page_pdf  # no re-encode when nothing is sliced
    assert total == 2
    assert page_indices == [0, 1]
