"""Documents and feeds through ``read``: detection, conversion, the zip/XML guards."""

import asyncio
import concurrent.futures as cf
import contextlib
import io
import json
import threading
import time
import zipfile

import pytest

from argus.extract import docs
from argus.extract.docs import DocError, convert, sniff

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _para(text: str, style: str | None = None, numbered: bool = False) -> str:
    ppr = ""
    if style or numbered:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "") + (
            "<w:numPr><w:numId w:val='1'/></w:numPr>" if numbered else "") + "</w:pPr>"
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx(body: str, prolog: str = "") -> bytes:
    xml = f'{prolog}<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    return _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": xml})


DOCX = _docx(
    _para("Quarterly Report", "Title") + _para("Overview", "Heading2")
    + "<w:p><w:r><w:t>Line one</w:t><w:tab/><w:t>tabbed</w:t><w:br/><w:t>broken</w:t></w:r></w:p>"
    + _para("first point", numbered=True)
    + "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Site</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>kWh</w:t></w:r></w:p></w:tc></w:tr>"
    "<w:tr><w:tc><w:p><w:r><w:t>Batam</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>1200</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
)


def _slide(text: str) -> str:
    return (f'<p:sld xmlns:p="x" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody>'
            f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>")


PPTX = _zip({"ppt/presentation.xml": "<p/>", "ppt/slides/slide2.xml": _slide("Second slide"),
             "ppt/slides/slide10.xml": _slide("Tenth slide"),
             "ppt/slides/slide1.xml": _slide("First slide")})


def _xlsx(rows: list[str]) -> bytes:
    return _zip({
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
                           f'<sheet name="Energy" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst xmlns="{S}"><si><t>Meter</t></si><si><t>kWh</t></si>'
                                f"<si><r><t>Pan</t></r><r><t>el A</t></r></si></sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData>{"".join(rows)}'
                                    f"</sheetData></worksheet>",
    })


XLSX = _xlsx([
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>',
    '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>12.50</v></c></row>',
    '<row r="3"><c r="A3" t="inlineStr"><is><t>Panel B</t></is></c><c r="C3"><v>7</v></c></row>',
])


def _epub(extra: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:'
                   'opendocument:xmlns:container"><rootfiles><rootfile full-path="c.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("c.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/'
                   'dc/elements/1.1/"><dc:title>Probe Book</dc:title><dc:identifier id="id">x'
                   '</dc:identifier></metadata><manifest><item id="c1" href="c1.xhtml" '
                   'media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/>'
                   "</spine></package>")
        z.writestr("c1.xhtml",
                   '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
                   "<title>c1</title></head><body><h1>Chapter One</h1>"
                   "<p>The epub body alpha text.</p>" + extra + "</body></html>")
    return buf.getvalue()


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<title>Grid News</title>
<item><title>Tariff update</title><link>https://ex.test/a</link>
<pubDate>Fri, 30 Oct 2026 08:00:00 GMT</pubDate><dc:creator>PLN Desk</dc:creator>
<description>&lt;p&gt;New &lt;b&gt;rates&lt;/b&gt; apply.&lt;/p&gt;</description></item>
<item><title>Second</title><link>https://ex.test/b</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Log</title>
<entry><title>Release 1.2</title><link rel="alternate" href="https://ex.test/r12"/>
<updated>2026-10-29T10:00:00Z</updated><author><name>Kemal</name></author>
<summary>Faster gateway.</summary></entry></feed>"""

RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://ex.test/"><title>RDF Feed</title></channel>
<item rdf:about="https://ex.test/x"><title>RDF item</title><link>https://ex.test/x</link>
<dc:date>2026-10-01</dc:date></item></rdf:RDF>"""


# --- conversion, one happy path per type ----------------------------------------------


def test_docx_headings_lists_breaks_and_tables():
    r = convert("docx", DOCX, "https://ex.test/r.docx", "markdown")
    c = r["content"]
    assert r["metadata"]["doc_type"] == "docx"
    assert "# Quarterly Report" in c and "## Overview" in c
    assert "Line one\ttabbed\nbroken" in c
    assert "- first point" in c
    assert "| Site | kWh |" in c and "| Batam | 1200 |" in c
    assert r["truncated"] is False


def test_pptx_slides_in_numeric_order():
    c = convert("pptx", PPTX, "https://ex.test/d.pptx", "markdown")["content"]
    assert c.index("## Slide 1") < c.index("First slide") < c.index("## Slide 2")
    assert c.index("Second slide") < c.index("## Slide 10") < c.index("Tenth slide")


def test_xlsx_shared_inline_and_numbers_as_written():
    r = convert("xlsx", XLSX, "https://ex.test/m.xlsx", "markdown")
    c = r["content"]
    assert "## Energy" in c
    assert "| Meter | kWh |  |" in c
    assert "| Panel A | 12.50 |  |" in c  # rich-text runs joined; number kept as written
    assert "| Panel B |  | 7 |" in c  # inline string; sparse cell lands in column C
    assert r["metadata"]["rows_total"] == {"Energy": 3}


def test_epub_to_markdown():
    r = convert("epub", _epub(), "https://ex.test/b.epub", "markdown")
    assert "Chapter One" in r["content"] and "epub body alpha" in r["content"]
    assert r["title"] == "Probe Book"


def test_csv_semicolon_to_table():
    r = convert("csv", "site;kwh\nBatam;12\nJakarta;30\n", "https://ex.test/x.csv", "markdown")
    assert r["content"].splitlines()[:3] == ["| site | kwh |", "|---|---|", "| Batam | 12 |"]
    assert r["metadata"]["rows_total"] == 3


def test_json_pretty_fenced():
    r = convert("json", '{"a":[1,2],"b":"x"}', "https://ex.test/x.json", "markdown")
    assert r["content"] == '```json\n{\n  "a": [\n    1,\n    2\n  ],\n  "b": "x"\n}\n```'


def test_plain_xml_fenced_not_parsed():
    xml = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]><cfg><v>&e;</v></cfg>'
    r = convert("xml", xml, "https://ex.test/c.xml", "markdown")
    assert r["content"] == f"```xml\n{xml}\n```"


def test_rss_feed_items_and_metadata():
    r = convert("feed", RSS, "https://ex.test/rss", "markdown")
    c = r["content"]
    assert c.startswith("# Grid News")
    assert ("- [Tariff update](https://ex.test/a) - Fri, 30 Oct 2026 08:00:00 GMT - PLN Desk"
            in c)
    assert "  New rates apply." in c
    items = r["metadata"]["items"]
    assert items[0] == {"title": "Tariff update", "url": "https://ex.test/a",
                        "published": "Fri, 30 Oct 2026 08:00:00 GMT",
                        "summary": "New rates apply."}
    assert items[1]["published"] == "" and r["title"] == "Grid News"


def test_atom_and_rdf_feeds():
    a = convert("feed", ATOM, "https://ex.test/atom", "markdown")
    assert "- [Release 1.2](https://ex.test/r12) - 2026-10-29T10:00:00Z - Kemal" in a["content"]
    assert a["metadata"]["items"][0]["summary"] == "Faster gateway."
    r = convert("feed", RDF, "https://ex.test/rdf", "markdown")
    assert r["title"] == "RDF Feed"
    assert "- [RDF item](https://ex.test/x) - 2026-10-01" in r["content"]


def test_text_and_html_formats():
    t = convert("docx", DOCX, "https://ex.test/r.docx", "text")["content"]
    assert "Quarterly Report" in t and "# " not in t
    h = convert("csv", "a,b\n1,2\n", "https://ex.test/x.csv", "html")
    assert "| a | b |" in h["content"] and "markdown" in h["metadata"]["format_note"]


# --- detection -------------------------------------------------------------------------


@pytest.mark.parametrize(("ctype", "kind"), [
    ("application/pdf", "pdf"), (DOCX_CT, "docx"), ("application/epub+zip", "epub"),
    ("text/csv", "csv"), ("application/json", "json"), ("application/ld+json", "json"),
    ("application/rss+xml", "feed"), ("application/atom+xml", "feed"),
])
def test_sniff_by_content_type(ctype, kind):
    body = b"" if kind not in ("pdf", "docx", "epub") else b"bytes"
    text = RSS if kind == "feed" else "{}"
    assert sniff(ctype, body, text, "https://ex.test/x") == kind


def test_sniff_xml_root_decides_feed():
    assert sniff("text/xml", None, RSS, "https://ex.test/x") == "feed"
    assert sniff("application/xml", None, RDF, "https://ex.test/x") == "feed"
    assert sniff("text/xml", None, "<!-- <rss> --><cfg/>", "https://ex.test/x") == "xml"
    assert sniff("text/xml", None, "<html><body>hi</body></html>", "https://ex.test/x") is None


def test_sniff_magic_bytes_and_extension():
    assert sniff("application/octet-stream", DOCX, "", "https://ex.test/dl") == "zip"
    assert sniff("", b"junk\n%PDF-1.7", "", "https://ex.test/dl") == "pdf"
    assert sniff("text/plain", None, "a,b", "https://ex.test/d.CSV?x=1") == "csv"
    assert sniff("", None, "{}", "https://ex.test/d.json") == "json"
    assert sniff("", None, RSS, "https://ex.test/news.rss") == "feed"
    # a binary type by extension needs the raw bytes; text served at .docx is not one
    assert sniff("text/plain", None, "hi", "https://ex.test/f.docx") is None


def test_sniff_leaves_html_alone():
    assert sniff("text/html", DOCX, "<html/>", "https://ex.test/a.pdf") is None
    assert sniff("application/xhtml+xml", None, "<html/>", "https://ex.test/a") is None
    assert sniff("", None, "<html/>", "https://ex.test/page") is None


def test_zip_content_decides_the_office_type():
    assert convert("zip", XLSX, "https://ex.test/dl", "markdown")["metadata"]["doc_type"] == "xlsx"
    # served as docx, built as pptx: the parts inside win
    assert convert("docx", PPTX, "https://ex.test/dl", "markdown")[
        "metadata"]["doc_type"] == "pptx"
    with pytest.raises(DocError) as e:
        convert("zip", _zip({"readme.txt": "hi"}), "https://ex.test/a.zip", "markdown")
    assert e.value.reason == "unsupported_document"


# --- guards ----------------------------------------------------------------------------


def test_zip_declared_size_cap(monkeypatch):
    monkeypatch.setattr(docs, "MAX_UNZIPPED", 1000)
    data = _docx(_para("x" * 2000))
    with pytest.raises(DocError) as e:
        convert("docx", data, "https://ex.test/r.docx", "markdown")
    assert e.value.reason == "doc_too_large"


def test_zip_compression_ratio_guard():
    bomb = _zip({"word/document.xml": b"\0" * 3_000_000})  # ~1000:1
    with pytest.raises(DocError) as e:
        convert("docx", bomb, "https://ex.test/r.docx", "markdown")
    assert e.value.reason == "doc_too_large" and "ratio" in e.value.detail


def test_zip_too_many_entries(monkeypatch):
    monkeypatch.setattr(docs, "MAX_ENTRIES", 5)
    data = _zip({f"f{i}": "x" for i in range(6)})
    with pytest.raises(DocError) as e:
        convert("zip", data, "https://ex.test/a.zip", "markdown")
    assert e.value.reason == "doc_too_large"


def test_zip_entry_read_is_capped(monkeypatch):
    monkeypatch.setattr(docs, "MAX_ENTRY_BYTES", 100)
    with pytest.raises(DocError) as e:
        convert("docx", _docx(_para("y" * 500)), "https://ex.test/r.docx", "markdown")
    assert e.value.reason == "doc_too_large"


def test_xxe_external_entity_not_resolved(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-MARKER")
    uri = secret.resolve().as_uri()
    dtd = f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY xxe SYSTEM "{uri}">]>'
    r = convert("docx", _docx(_para("&xxe;") + _para("visible"), prolog=dtd),
                "https://ex.test/r.docx", "markdown")
    assert "TOPSECRET" not in r["content"] and "visible" in r["content"]
    feed = RSS.replace('<?xml version="1.0" encoding="UTF-8"?>',
                       f'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "{uri}">]>').replace(
        "Grid News", "&xxe;")
    r = convert("feed", feed, "https://ex.test/rss", "markdown")
    assert "TOPSECRET" not in r["content"] and "TOPSECRET" not in str(r["metadata"])


def test_billion_laughs_does_not_expand():
    ents = '<!ENTITY lol "lol">' + "".join(
        f'<!ENTITY lol{i} "{(f"&lol{i - 1};" if i > 1 else "&lol;") * 10}">' for i in range(1, 10))
    feed = f'<?xml version="1.0"?><!DOCTYPE rss [{ents}]>' + (
        "<rss><channel><title>&lol9;</title><item><title>t</title></item></channel></rss>")
    t0 = time.monotonic()
    try:
        r = convert("feed", feed, "https://ex.test/rss", "markdown")
    except Exception:  # noqa: BLE001 - refusing the document is a pass too
        r = {"content": ""}
    assert "lol" * 50 not in r["content"] and time.monotonic() - t0 < 5


# --- truncation is flagged, never silent ----------------------------------------------


def test_csv_row_cap_is_flagged():
    text = "n\n" + "".join(f"{i}\n" for i in range(2500))
    r = convert("csv", text, "https://ex.test/x.csv", "markdown")
    assert r["truncated"] is True
    assert (r["metadata"]["rows_total"], r["metadata"]["rows_returned"]) == (2501, 2000)
    assert "| 1998 |" in r["content"] and "| 1999 |" not in r["content"]


def test_feed_item_cap_is_flagged():
    items = "".join(f"<item><title>i{n}</title></item>" for n in range(205))
    r = convert("feed", f"<rss><channel><title>F</title>{items}</channel></rss>",
                "https://ex.test/rss", "markdown")
    assert r["truncated"] is True and len(r["metadata"]["items"]) == 200
    assert r["metadata"]["items_total"] == 205


# --- off the event loop: the extraction worker pool ------------------------------------


@pytest.fixture
def one_worker(monkeypatch):
    from argus.extract import pool

    monkeypatch.setenv("ARGUS_EXTRACT_WORKERS", "1")
    pool.shutdown()
    yield pool
    pool.shutdown()


async def test_convert_async_on_a_spawned_worker(one_worker):
    r = await docs.convert_async("zip", XLSX, "https://ex.test/dl", "markdown")
    assert r["metadata"]["doc_type"] == "xlsx" and one_worker._doc_pool is not None
    with pytest.raises(DocError) as e:  # the guard's error survives the process boundary
        await docs.convert_async("docx", _zip({"word/document.xml": b"\0" * 3_000_000}),
                                 "https://ex.test/r.docx", "markdown")
    assert e.value.reason == "doc_too_large"


async def test_convert_async_timeout_recycles_the_pool(one_worker, monkeypatch):
    monkeypatch.setattr(one_worker, "_DOC_TIMEOUT_S", 0.01)  # a spawn alone takes longer
    with pytest.raises(TimeoutError):
        await docs.convert_async("docx", DOCX, "https://ex.test/r.docx", "markdown")
    assert one_worker._doc_pool is None


# --- read() end to end -----------------------------------------------------------------


async def test_fetch_static_keeps_bytes_for_documents_only(public_dns):
    import httpx

    from argus.fetch.static import fetch_static

    def h(req):
        if req.url.path == "/dl":
            return httpx.Response(200, content=DOCX,
                                  headers={"content-type": "Application/Octet-Stream; x=1"})
        return httpx.Response(200, text="<html><body>hi</body></html>",
                              headers={"content-type": "text/html; charset=utf-8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        doc = await fetch_static("https://ex.test/dl", client=c, api=True)
        page = await fetch_static("https://ex.test/page", client=c, api=True)
    assert doc["content_type"] == "application/octet-stream" and doc["body"] == DOCX
    assert isinstance(doc["html"], str)
    assert page["content_type"] == "text/html" and "body" not in page


async def test_read_converts_docx_and_caches(app_state, monkeypatch):
    from argus import server

    calls = []

    async def fake_fetch(url, **kw):
        calls.append(url)
        return {"final_url": url, "status": 200, "html": DOCX.decode("latin-1"),
                "render_path": "static", "content_type": DOCX_CT, "body": DOCX}

    monkeypatch.setattr(server, "fetch", fake_fetch)
    r = await server.read("https://ex.test/report")
    assert r["metadata"]["doc_type"] == "docx" and "# Quarterly Report" in r["content"]
    assert r["title"] is None and r["format"] == "markdown" and r["from_cache"] is False
    assert set(r) >= {"url", "final_url", "status", "render_path", "metadata"}
    again = await server.read("https://ex.test/report")
    assert again["from_cache"] is True and len(calls) == 1


async def test_read_feed_via_text_xml(app_state, monkeypatch):
    from argus import server

    async def fake_fetch(url, **kw):
        return {"final_url": url, "status": 200, "html": RSS, "render_path": "static",
                "content_type": "text/xml"}

    monkeypatch.setattr(server, "fetch", fake_fetch)
    r = await server.read("https://ex.test/feed", max_chars=20)
    assert r["metadata"]["doc_type"] == "feed" and r["truncated"] is True
    assert r["metadata"]["items"][0]["url"] == "https://ex.test/a"
    # the cap reaches the item list too: only items whose line starts inside the cut
    # (2026-10-31: a 1200-char read of Google News still carried 100 items)
    assert len(r["metadata"]["items"]) == 1 and r["metadata"]["items_returned"] == 1
    assert r["metadata"]["items_total"] == 2
    full = await server.read("https://ex.test/feed")
    assert len(full["metadata"]["items"]) == 2 and "truncated" not in full


async def test_read_guard_violation_is_structured_error(app_state, monkeypatch):
    from argus import server

    bomb = _zip({"word/document.xml": b"\0" * 3_000_000})

    async def fake_fetch(url, **kw):
        return {"final_url": url, "status": 200, "html": "", "render_path": "static",
                "content_type": "application/octet-stream", "body": bomb}

    monkeypatch.setattr(server, "fetch", fake_fetch)
    r = await server.read("https://ex.test/bomb")
    assert r["code"] == "doc_too_large" and r["retryable"] is False


async def test_read_pdf_url_uses_pdf_extractor(app_state):
    from argus import server
    from conftest import BASE

    r = await server.read(f"{BASE}/doc.pdf")
    assert r["metadata"]["doc_type"] == "pdf" and "page one alpha" in r["content"]
    assert r["metadata"]["pages_total"] == 2 and "truncated" not in r


async def test_read_html_unchanged(app_state):
    from argus import server
    from conftest import BASE

    r = await server.read(f"{BASE}/article")
    assert "doc_type" not in r["metadata"] and "Gold prices" in r["content"]


def test_pptx_keeps_picture_alt_text_in_slide_order():
    """Authors put captions and facts in a picture's `descr` (markitdown's fixture does)."""
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    slide = (f'<p:sld xmlns:p="{p}" xmlns:a="{A}"><p:cSld><p:spTree>'
             "<p:sp><p:nvSpPr><p:cNvPr id='2' name='Title' descr='not a picture'/></p:nvSpPr>"
             "<p:txBody><a:p><a:r><a:t>Before</a:t></a:r></a:p></p:txBody></p:sp>"
             "<p:pic><p:nvPicPr><p:cNvPr id='5' name='Picture 4' descr='Chart of &lt;kWh&gt;'/>"
             "</p:nvPicPr></p:pic>"
             "<p:sp><p:txBody><a:p><a:r><a:t>After</a:t></a:r></a:p></p:txBody></p:sp>"
             "</p:spTree></p:cSld></p:sld>")
    data = _zip({"ppt/presentation.xml": "<p/>", "ppt/slides/slide1.xml": slide})
    c = convert("pptx", data, "https://ex.test/d.pptx", "markdown")["content"]
    assert c.index("Before") < c.index("![Chart of <kWh>](image)") < c.index("After")
    assert "not a picture" not in c


def _served(html: str, ctype: str, body: bytes | None = None):
    async def fake_fetch(url, **kw):
        res = {"final_url": url, "status": 200, "html": html, "render_path": "static",
               "content_type": ctype}
        if body is not None:
            res["body"] = body
        return res
    return fake_fetch


@pytest.mark.parametrize(("ctype", "url"), [
    ("application/json", "https://ex.test/api"),          # a broken JSON API answer
    ("text/plain", "https://raw.ex.test/repo/tsconfig.json"),  # comments: not strict JSON
])
async def test_read_text_that_is_not_valid_json_falls_back_to_the_page(app_state, monkeypatch,
                                                                       ctype, url):
    from argus import server

    text = '{\n  // compiler options\n  "strict": true, "target": "es2022",\n}'
    monkeypatch.setattr(server, "fetch", _served(text, ctype))
    r = await server.read(url)
    assert "error" not in r, r
    assert "strict" in r["content"] and "doc_type" not in r["metadata"]


async def test_read_corrupt_docx_is_parse_failed_not_not_pdf(app_state, monkeypatch):
    from argus import server

    junk = b"PK\x03\x04" + b"\x00garbage" * 50
    monkeypatch.setattr(server, "fetch", _served(junk.decode("latin-1"), DOCX_CT, junk))
    r = await server.read("https://ex.test/broken.docx")
    assert r["code"] == "parse_failed"


async def test_read_non_pdf_bytes_at_pdf_type_is_not_pdf(app_state, monkeypatch):
    from argus import server

    body = b"%PDF-1.7 this is not really a pdf"
    monkeypatch.setattr(server, "fetch", _served(body.decode(), "application/pdf", body))
    r = await server.read("https://ex.test/fake.pdf")
    assert r["code"] == "not_pdf"


async def test_read_document_skips_extract_media_and_counts_words(app_state, monkeypatch):
    from argus import server

    monkeypatch.setattr(server, "fetch", _served(DOCX.decode("latin-1"), DOCX_CT, DOCX))
    r = await server.read("https://ex.test/report.docx", extract_media=True)
    assert "links" not in r and "images" not in r
    assert r["metadata"]["word_count"] > 0


# --- review 2026-10-31: amplification and DoS regressions (each < 2 s) -----------------

_SNS = f'xmlns="{S}"'


def _sheet_zip(sheet_xml: str, sheets: int = 1) -> bytes:
    """A workbook whose ``sheets`` sheets all point at one worksheet part."""
    return _zip({
        "xl/workbook.xml": f'<workbook {_SNS} xmlns:r="{R}"><sheets>' + "".join(
            f'<sheet name="s{i}" sheetId="{i}" r:id="rId1"/>' for i in range(1, sheets + 1))
            + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels":
            '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        "xl/worksheets/sheet1.xml": f"<worksheet {_SNS}><sheetData>{sheet_xml}</sheetData>"
                                    "</worksheet>",
    })


def _col_name(i: int) -> str:
    s = ""
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def test_xlsx_huge_column_ref_is_linear():
    """80k-letter ref took 3.2 s of big-int math from a 574-byte zip; > 3 letters is invalid."""
    data = _sheet_zip(f'<row><c r="{"A" * 80_000}1"><v>5</v></c><c r="B1"><v>6</v></c></row>')
    t0 = time.monotonic()
    r = convert("xlsx", data, "https://ex.test/m.xlsx", "markdown")
    assert time.monotonic() - t0 < 0.5
    assert "| 5 | 6 |" in r["content"]  # the bad ref falls back to its position


def test_xlsx_aliased_sheets_parsed_once_and_capped(monkeypatch):
    """300 sheets aliasing one 1 MB part re-parsed it 300 times (42 s from 6.5 KB)."""
    parsed = []
    real = docs._xlsx_rows
    monkeypatch.setattr(docs, "_xlsx_rows", lambda d, s: parsed.append(len(d)) or real(d, s))
    data = _sheet_zip('<row><c r="A1"><v>1</v></c></row>', sheets=60)
    r = convert("xlsx", data, "https://ex.test/m.xlsx", "markdown")
    m = r["metadata"]
    assert (m["sheets_total"], m["sheets_returned"]) == (60, 50) and r["truncated"] is True
    assert len(parsed) == 1  # the one worksheet, streamed once
    assert r["content"].count("(same worksheet as s1)") == 49


def test_zip_total_read_budget(monkeypatch):
    monkeypatch.setattr(docs, "MAX_READ_BYTES", 300)
    data = _docx(_para("z" * 400))
    with pytest.raises(DocError) as e:
        convert("docx", data, "https://ex.test/r.docx", "markdown")
    assert e.value.reason == "doc_too_large" and "add up" in e.value.detail


def test_csv_wide_row_does_not_pad_every_row():
    """One 20k-field row + 1999 short rows: 44 KB in, 120 MB of padded markdown out."""
    text = ",".join(["a"] * 20_000) + "\n" + "\n".join(["x"] * 1999)
    t0 = time.monotonic()
    r = convert("csv", text, "https://ex.test/x.csv", "markdown")
    assert time.monotonic() - t0 < 1
    m = r["metadata"]
    assert (m["cols_total"], m["cols_returned"]) == (20_000, docs.MAX_COLS)
    assert r["truncated"] is True and len(r["content"]) < 2_000_000


def test_xlsx_sparse_columns_capped():
    """2000 rows x 8 cells, each in a new column: 16k columns, 96 MB out from 44 KB."""
    rows = "".join(
        "<row>" + "".join(f'<c r="{_col_name(r * 8 + j + 1)}{r + 1}"><v>1</v></c>'
                          for j in range(8)) + "</row>" for r in range(2000))
    t0 = time.monotonic()
    r = convert("xlsx", _sheet_zip(rows), "https://ex.test/m.xlsx", "markdown")
    assert time.monotonic() - t0 < 1.5
    assert (r["metadata"]["cols_total"], r["metadata"]["cols_returned"]) == (16_000, 200)
    assert r["truncated"] is True and len(r["content"]) < 2_000_000


def test_json_deep_or_large_is_not_reindented(monkeypatch):
    """10 KB nested 100 deep came out at 1 MB once indented; it now stays as served."""
    deep = "[" * 100 + ",".join(["1"] * 5000) + "]" * 100
    r = convert("json", deep, "https://ex.test/x.json", "markdown")
    assert r["content"] == f"```json\n{deep}\n```"
    monkeypatch.setattr(docs, "_JSON_PRETTY_MAX_BYTES", 10)
    flat = '{"a": [1, 2]}'
    assert convert("json", flat, "u", "markdown")["content"] == f"```json\n{flat}\n```"
    assert docs._json_depth(json.loads("[" * 900 + "]" * 900)) == 17  # iterative, stops early


def test_content_char_cap_is_flagged(monkeypatch):
    monkeypatch.setattr(docs, "MAX_CHARS", 50)
    xml = "<cfg>" + "v" * 200 + "</cfg>"
    r = convert("xml", xml, "https://ex.test/c.xml", "markdown")
    m = r["metadata"]
    assert len(r["content"]) == 50 and r["truncated"] is True
    assert (m["chars_total"], m["chars_returned"]) == (len(f"```xml\n{xml}\n```"), 50)


async def test_document_timeout_leaves_article_pool_intact(one_worker, monkeypatch):
    """A document overrunning used to kill the shared pool research/batch_read extract on."""
    article = one_worker._get_pool()
    monkeypatch.setattr(one_worker, "_DOC_TIMEOUT_S", 0.01)
    with pytest.raises(TimeoutError):
        await docs.convert_async("docx", DOCX, "https://ex.test/r.docx", "markdown")
    assert one_worker._pool is article and not article._broken
    assert one_worker._doc_pool is None


async def test_read_pdf_runs_on_the_document_pool(app_state, monkeypatch):
    """read's PDFs no longer queue on the single uncancellable PDF thread read_pdf uses."""
    from argus import server
    from argus.extract import pool
    from conftest import PDF_BYTES

    seen = []
    real = pool.run_document

    async def spy(fn, *args, timeout=None):
        seen.append((fn.__name__, timeout))
        return await real(fn, *args, timeout=timeout)

    monkeypatch.setattr(pool, "run_document", spy)
    monkeypatch.setattr(server, "fetch", _served("", "application/pdf", PDF_BYTES))
    r = await server.read("https://ex.test/a.pdf", timeout=7)
    assert seen == [("extract_pdf", 7)] and r["metadata"]["doc_type"] == "pdf"


def test_not_pdf_survives_the_process_boundary():
    """A worker's exception reaches the caller pickled; not_pdf must come back intact."""
    import pickle

    from argus.extract.pdf import extract_pdf

    with pytest.raises(ValueError) as e:
        extract_pdf(b"%PDF-1.7 nope")
    assert str(pickle.loads(pickle.dumps(e.value))) == "not_pdf"


def test_epub_pages_capped_and_counted(monkeypatch):
    from argus import config

    monkeypatch.setattr(config, "PDF_MAX_PAGES", 1)
    data = _epub("<p>filler line for another page of the book.</p>" * 20)  # 2 pages
    r = convert("epub", data, "https://ex.test/b.epub", "markdown")
    assert (r["metadata"]["pages_total"], r["metadata"]["pages_returned"]) == (2, 1)
    assert r["truncated"] is True


def test_feed_only_web_links_become_links():
    feed = ("<rss><channel><title>F</title>"
            "<item><title>Bad</title><link>javascript:alert(1)</link></item>"
            "<item><title>Data</title><link>data:text/html,x</link></item>"
            "<item><title>Good</title><link>HTTPS://ex.test/g</link></item></channel></rss>")
    r = convert("feed", feed, "https://ex.test/rss", "markdown")
    assert "javascript" not in r["content"] and "data:" not in r["content"]
    assert "- Bad" in r["content"] and "- [Good](HTTPS://ex.test/g)" in r["content"]
    assert [i["url"] for i in r["metadata"]["items"]] == ["", "", "HTTPS://ex.test/g"]


# --- re-verification residuals R1-R5 (each < 2 s) --------------------------------------


def test_xlsx_worksheet_streams_rows_and_counts_the_rest():
    """R1: findall(row) built every row of a 31 MB part (~1 GB per worker); now streamed."""
    rows = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 2501))
    r = convert("xlsx", _sheet_zip(rows + "<row/>" * 3000), "https://ex.test/m.xlsx",
                "markdown")
    m = r["metadata"]
    assert (m["rows_total"], m["rows_returned"]) == ({"s1": 5500}, {"s1": 2000})
    assert "| 2000 |" in r["content"] and "| 2001 |" not in r["content"]
    assert r["truncated"] is True
    # rows past the cap are counted on the bytes: a prefix counts, <rowBreaks> does not
    rows = "".join(f'<x:row r="{i}"><x:c r="A{i}"><x:v>1</x:v></x:c></x:row>'
                   for i in range(1, 2004))
    data = _zip({"xl/workbook.xml": f'<workbook {_SNS}><sheets><sheet name="p"/></sheets>'
                                    "</workbook>",
                 "xl/worksheets/sheet1.xml": f'<x:worksheet xmlns:x="{S}"><x:sheetData>{rows}'
                                             "</x:sheetData><x:rowBreaks/></x:worksheet>"})
    m = convert("xlsx", data, "https://ex.test/p.xlsx", "markdown")["metadata"]
    assert (m["rows_total"], m["rows_returned"]) == ({"p": 2003}, {"p": 2000})


def test_docx_table_rows_capped_and_flagged():
    """R4: a 300k-row DOCX table converted every row (7.7 s) and was not flagged."""
    trs = "".join(f"<w:tr><w:tc><w:p><w:r><w:t>r{i}</w:t></w:r></w:p></w:tc></w:tr>"
                  for i in range(2005))
    r = convert("docx", _docx(f"<w:tbl>{trs}</w:tbl>" + _para("after")),
                "https://ex.test/t.docx", "markdown")
    m = r["metadata"]
    assert (m["rows_total"], m["rows_returned"]) == (2005, 2000) and r["truncated"] is True
    assert "| r1999 |" in r["content"] and "r2000" not in r["content"]
    assert "after" in r["content"]


def test_feed_link_cannot_break_out_of_markdown():
    """R5: `)[evil](` in a link, or `]` in a title, used to inject a second link."""
    feed = ("<rss><channel><title>F</title><item><title>a [b] c</title>"
            "<link>https://ok.example/x)[evil](http://z y&lt;</link></item></channel></rss>")
    c = convert("feed", feed, "https://ex.test/rss", "markdown")["content"]
    assert "- [a \\[b\\] c](https://ok.example/x%29[evil]%28http://z%20y%3C)" in c
    assert c.count("](") == 1


class _ThreadDocPool(cf.ThreadPoolExecutor):
    """Stands in for the spawn pool (spawning costs seconds): same submit/shutdown surface."""

    _processes: dict = {}

    def __init__(self, n, mp_context=None):
        super().__init__(n)


@pytest.fixture
def thread_doc_pool(monkeypatch):
    from argus.extract import pool

    monkeypatch.setenv("ARGUS_EXTRACT_WORKERS", "1")  # one slot
    monkeypatch.setattr(pool.cf, "ProcessPoolExecutor", _ThreadDocPool)
    pool.shutdown()
    yield pool
    pool.shutdown()


async def _slot_freed(p) -> bool:
    for _ in range(100):
        if p._gate()._value == 1:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_job_on_a_pool_another_job_killed_is_retried_once(thread_doc_pool):
    """R2: a neighbour's timeout killed the pool under an innocent job, which then failed."""
    p, pools = thread_doc_pool, []

    def victim():
        pools.append(p._doc_pool)
        if len(pools) == 1:
            p._retire_doc(p._doc_pool)  # the bomb next door timed out
            raise cf.process.BrokenProcessPool("killed under us")
        return "ok"

    assert await p.run_document(victim) == "ok"
    assert len(pools) == 2 and pools[0] is not pools[1]


async def test_job_that_breaks_its_own_pool_is_not_retried(thread_doc_pool):
    p, calls = thread_doc_pool, []

    def bomb():
        calls.append(1)
        raise cf.process.BrokenProcessPool("this job killed the worker")

    with pytest.raises(cf.process.BrokenProcessPool):
        await p.run_document(bomb)
    assert calls == [1] and p._doc_pool is None and await _slot_freed(p)


async def test_cancelled_caller_keeps_the_slot_until_the_job_ends(thread_doc_pool):
    """R3: a disconnect freed the slot while the worker ran on; orphans starved later jobs."""
    p, done = thread_doc_pool, threading.Event()
    task = asyncio.create_task(p.run_document(done.wait, 5))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert p._gate()._value == 0  # the job still runs, so its slot is still taken
    done.set()
    assert await _slot_freed(p)
    assert await p.run_document(int, "3") == 3  # no leak, no deadlock


async def test_watchdog_kills_an_orphan_past_its_deadline(thread_doc_pool, monkeypatch):
    p, done, killed = thread_doc_pool, threading.Event(), []
    real = p._retire_doc

    def kill(pool):  # the real kill, plus ending the stand-in "worker" it would have killed
        killed.append(pool)
        real(pool)
        done.set()

    monkeypatch.setattr(p, "_retire_doc", kill)
    monkeypatch.setattr(p, "_WATCHDOG_GRACE_S", 0)
    task = asyncio.create_task(p.run_document(done.wait, 5, timeout=0.2))
    await asyncio.sleep(0.05)
    pool_used = p._doc_pool
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert p._gate()._value == 0 and not killed
    await asyncio.sleep(0.3)
    assert killed == [pool_used] and await _slot_freed(p)


def test_xlsx_rows_with_mixed_prefixes_cannot_hide_truncation():
    """L1: counting only the last kept row's prefix let `<b:row>` rows past the cap vanish."""
    head = "".join(f'<a:row r="{i}"><a:c r="A{i}"><a:v>1</a:v></a:c></a:row>'
                   for i in range(1, 2001))
    tail = "".join(f'<b:row r="{i}"/>' for i in range(2001, 2501))
    data = _zip({"xl/workbook.xml": f'<workbook {_SNS}><sheets><sheet name="m"/></sheets>'
                                    "</workbook>",
                 "xl/worksheets/sheet1.xml": f'<a:worksheet xmlns:a="{S}" xmlns:b="{S}">'
                                             f"<a:sheetData>{head}{tail}</a:sheetData>"
                                             "</a:worksheet>"})
    r = convert("xlsx", data, "https://ex.test/m.xlsx", "markdown")
    assert r["metadata"]["rows_total"] == {"m": 2500} and r["truncated"] is True


async def test_gate_slot_is_released_when_the_pool_cannot_be_built(thread_doc_pool, monkeypatch):
    """L2: a failure building the pool after taking the slot leaked it for good."""
    p = thread_doc_pool

    def boom():
        raise OSError("no processes")

    monkeypatch.setattr(p, "_get_doc_pool", boom)
    with pytest.raises(OSError):
        await p.run_document(int, "1")
    assert await _slot_freed(p)


# --- PPTX charts and CSV charsets --------------------------------------------------------

C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_CHART_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"


def _chart_slide(before: str, after: str, rid: str = "rId2") -> str:
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    return (f'<p:sld xmlns:p="{p}" xmlns:a="{A}" xmlns:c="{C}" xmlns:r="{R}"><p:cSld>'
            f"<p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{before}</a:t></a:r></a:p></p:txBody>"
            f'</p:sp><p:graphicFrame><a:graphic><a:graphicData uri="{C}"><c:chart r:id="{rid}"/>'
            "</a:graphicData></a:graphic></p:graphicFrame><p:sp><p:txBody><a:p><a:r>"
            f"<a:t>{after}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>")


def _ser(name: str, cats: list[str], vals: list[str]) -> str:
    pts = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(cats))
    nums = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(vals))
    return (f"<c:ser><c:tx><c:strRef><c:strCache><c:pt idx='0'><c:v>{name}</c:v></c:pt>"
            f"</c:strCache></c:strRef></c:tx><c:cat><c:strRef><c:strCache>{pts}</c:strCache>"
            f"</c:strRef></c:cat><c:val><c:numRef><c:numCache>{nums}</c:numCache></c:numRef>"
            "</c:val></c:ser>")


def _chart_xml(title: str, series: str) -> str:
    head = (f"<c:title><c:tx><c:rich><a:p><a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx>"
            "</c:title>") if title else ""
    return (f'<c:chartSpace xmlns:c="{C}" xmlns:a="{A}"><c:chart>{head}<c:plotArea>'
            f"<c:barChart>{series}</c:barChart></c:plotArea></c:chart></c:chartSpace>")


def _chart_pptx(chart: str, slides: int = 1) -> bytes:
    files = {"ppt/presentation.xml": "<p/>", "ppt/charts/chart1.xml": chart}
    for n in range(1, slides + 1):
        files[f"ppt/slides/slide{n}.xml"] = _chart_slide(f"before {n}", f"after {n}")
        files[f"ppt/slides/_rels/slide{n}.xml.rels"] = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'relationships"><Relationship Id="rId2" Type="{_CHART_REL}" '
            'Target="../charts/chart1.xml"/></Relationships>')
    return _zip(files)


def test_pptx_chart_title_and_table_in_slide_order():
    chart = _chart_xml("Energy by year", _ser("Batam", ["2001", "2002"], ["10", "12.5"])
                       + _ser("Jakarta", ["2001", "2002"], ["30", "31"]))
    r = convert("pptx", _chart_pptx(chart), "https://ex.test/d.pptx", "markdown")
    c = r["content"]
    assert c.index("before 1") < c.index("### Chart: Energy by year") < c.index("after 1")
    assert "| Category | Batam | Jakarta |" in c
    assert "| 2001 | 10 | 30 |" in c and "| 2002 | 12.5 | 31 |" in c
    assert r["truncated"] is False


def test_pptx_chart_rows_capped_and_aliased_chart_printed_once():
    n = docs.MAX_ROWS + 5
    chart = _chart_xml("", _ser("S", [str(i) for i in range(n)], ["1"] * n))
    r = convert("pptx", _chart_pptx(chart, slides=3), "https://ex.test/d.pptx", "markdown")
    m = r["metadata"]
    assert (m["rows_total"], m["rows_returned"]) == (n, docs.MAX_ROWS)
    assert r["truncated"] is True and "### Chart\n" in r["content"]
    assert r["content"].count("(same chart as slide 1)") == 2  # parsed and printed once


def test_pptx_chart_with_missing_part_keeps_the_slide():
    data = _zip({"ppt/presentation.xml": "<p/>",
                 "ppt/slides/slide1.xml": _chart_slide("kept", "also kept")})
    r = convert("pptx", data, "https://ex.test/d.pptx", "markdown")
    c, m = r["content"], r["metadata"]
    assert "kept" in c and "also kept" in c and "Chart" not in c
    # an unresolved chart is counted, so the read says something is missing
    assert (m["charts_total"], m["charts_returned"]) == (1, 0) and r["truncated"] is True


_KANJI = "名前,年齢,住所\n佐藤太郎,30,東京\n三木英子,25,大阪\n髙橋淳,35,名古屋\n"


@pytest.mark.parametrize(("path", "ctype"), [("/k.csv", "text/csv"),
                                             ("/k.csv", "text/plain"),
                                             ("/k.tsv", "text/plain")])
async def test_fetch_static_detects_csv_charset_without_header(public_dns, path, ctype):
    import httpx

    from argus.fetch.static import fetch_static

    body = _KANJI.encode("cp932")  # 髙 exists in cp932 only, not in plain Shift-JIS

    def h(req):
        return httpx.Response(200, content=body, headers={"content-type": ctype})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        res = await fetch_static(f"https://ex.test{path}", client=c, api=True)
    assert "髙橋淳" in res["html"] and "�" not in res["html"]
    if path.endswith(".csv"):
        out = convert("csv", res["html"], "https://ex.test/k.csv", "markdown")["content"]
        assert "| 髙橋淳 | 35 | 名古屋 |" in out


async def test_fetch_static_charset_guess_only_for_csv(public_dns):
    import httpx

    from argus.fetch.static import fetch_static

    utf8 = "名前,年齢\n佐藤,30\n".encode()
    cp932 = _KANJI.encode("cp932")
    routes = {"/u.csv": (utf8, "text/csv"), "/p.txt": (cp932, "text/plain"),
              "/j": (cp932, "application/json")}

    def h(req):
        body, ctype = routes[req.url.path]
        return httpx.Response(200, content=body, headers={"content-type": ctype})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        u = await fetch_static("https://ex.test/u.csv", client=c, api=True)
        p = await fetch_static("https://ex.test/p.txt", client=c, api=True)
        j = await fetch_static("https://ex.test/j", client=c, api=True)
    assert u["html"] == utf8.decode()  # valid utf-8 is never second-guessed
    assert "�" in p["html"] and "�" in j["html"]  # not a CSV: utf-8 as before
