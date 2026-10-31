"""OCR: ``read`` of an image URL and the image-only pages of scanned PDFs, via tesseract.

Tesseract is mocked everywhere except ``test_real_tesseract_reads_rendered_text``, which
runs only where the binary exists (the production image; skipped on Windows dev and CI).
"""

import io
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pymupdf
import pytest
from PIL import Image, ImageDraw

from argus.extract import docs, ocr
from argus.extract.docs import DocError, convert, sniff
from argus.extract.pdf import extract_pdf


def _png(size=(40, 20), mode="RGB", color="white") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def _gif(frames: int) -> bytes:
    imgs = [Image.new("L", (10, 10), 60 * i) for i in range(frames)]  # distinct, kept
    buf = io.BytesIO()
    imgs[0].save(buf, "GIF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def _fake_subprocess(monkeypatch, run):
    """Replace ``subprocess`` for ocr.py only: PyMuPDF probes tesseract through the real one."""
    monkeypatch.setattr(ocr, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))


@pytest.fixture
def fake_tesseract(monkeypatch):
    """tesseract present, answering ``.text``; ``.calls`` records every run."""
    fake = SimpleNamespace(calls=[], text="Recognised words")

    def run(args, **kw):
        fake.calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, stdout=fake.text.encode(), stderr=b"")

    monkeypatch.setattr(ocr, "tesseract", lambda: "/usr/bin/tesseract")
    _fake_subprocess(monkeypatch, run)
    return fake


@pytest.fixture
def no_tesseract(monkeypatch):
    monkeypatch.setattr(ocr, "tesseract", lambda: None)


# --- detection ------------------------------------------------------------------------


@pytest.mark.parametrize("ctype", ["image/png", "image/jpeg", "image/webp", "image/gif",
                                   "image/tiff", "image/bmp"])
def test_sniff_image_by_content_type(ctype):
    assert sniff(ctype, b"x", "", "https://ex.test/i") == "image"


@pytest.mark.parametrize("magic", [b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff\xe0", b"GIF89a",
                                   b"RIFF\0\0\0\0WEBPVP8 ", b"II*\0", b"MM\0*",
                                   b"BM6\0\0\0\0\0\0\x006\0\0\0(\0\0\0"])  # BMP + DIB 40
def test_sniff_image_by_magic_and_extension(magic):
    assert sniff("application/octet-stream", magic + b"rest", "", "https://ex.test/d") == "image"
    assert sniff("", b"??", "", "https://ex.test/scan.JPEG") == "image"
    assert sniff("text/plain", None, "x", "https://ex.test/scan.png") is None  # no bytes


# --- image OCR --------------------------------------------------------------------------


def test_image_ocr_runs_tesseract_on_grayscale_png(fake_tesseract):
    r = convert("image", _png(mode="RGB"), "https://ex.test/i.png", "markdown")
    assert r["content"] == "Recognised words"
    m = r["metadata"]
    assert m["doc_type"] == "image" and m["ocr"] == {"engine": "tesseract", "lang": "eng+ind"}
    (args, kw), = fake_tesseract.calls
    assert args == ["/usr/bin/tesseract", "stdin", "stdout", "-l", "eng+ind"]
    assert kw["timeout"] == ocr.OCR_TIMEOUT_S and not kw.get("shell")
    assert Image.open(io.BytesIO(kw["input"])).format == "PNG"
    assert Image.open(io.BytesIO(kw["input"])).mode == "L"
    assert r["truncated"] is False


def test_image_over_the_pixel_cap_is_refused_before_decoding(fake_tesseract, monkeypatch):
    monkeypatch.setattr(ocr, "MAX_PIXELS", 799)
    with pytest.raises(DocError) as e:
        convert("image", _png((40, 20)), "https://ex.test/i.png", "markdown")
    assert e.value.reason == "doc_too_large" and not fake_tesseract.calls


def test_animated_image_uses_the_first_frame_flagged(fake_tesseract):
    r = convert("image", _gif(3), "https://ex.test/a.gif", "markdown")
    m = r["metadata"]
    assert (m["frames_total"], m["frames_returned"]) == (3, 1) and r["truncated"] is True
    assert len(fake_tesseract.calls) == 1


def test_image_ocr_output_cap_is_flagged(fake_tesseract, monkeypatch):
    monkeypatch.setattr(docs, "MAX_CHARS", 5)
    r = convert("image", _png(), "https://ex.test/i.png", "markdown")
    assert r["content"] == "Recog" and r["truncated"] is True


def test_image_without_tesseract_is_unsupported(no_tesseract):
    with pytest.raises(DocError) as e:
        convert("image", _png(), "https://ex.test/i.png", "markdown")
    assert (e.value.reason, e.value.detail) == ("unsupported_document", "ocr unavailable")


def _slow(args, **kw):
    raise subprocess.TimeoutExpired(args, kw["timeout"])


def test_tesseract_timeout_and_failure(monkeypatch):
    monkeypatch.setattr(ocr, "tesseract", lambda: "tesseract")
    _fake_subprocess(monkeypatch, _slow)
    with pytest.raises(TimeoutError):
        ocr.ocr_png(b"png", 1)
    _fake_subprocess(monkeypatch, lambda args, **kw: subprocess.CompletedProcess(
        args, 1, stdout=b"", stderr=b"Error opening data file"))
    with pytest.raises(RuntimeError):
        ocr.ocr_png(b"png", 1)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_real_tesseract_reads_rendered_text():
    from PIL import ImageFont

    img = Image.new("L", (900, 160), 255)
    ImageDraw.Draw(img).text((20, 40), "Argus reads gateway", fill=0,
                             font=ImageFont.load_default(size=64))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = convert("image", buf.getvalue(), "https://ex.test/i.png", "markdown")
    assert "gateway" in r["content"].lower()


# --- read() ------------------------------------------------------------------------------


def _served(ctype: str, body: bytes):
    async def fake_fetch(url, **kw):
        return {"final_url": url, "status": 200, "html": body.decode("latin-1"),
                "render_path": "static", "content_type": ctype, "body": body}
    return fake_fetch


async def test_read_image_url_returns_ocr_text(app_state, monkeypatch, fake_tesseract):
    from argus import server

    monkeypatch.setattr(server, "fetch", _served("image/png", _png()))
    r = await server.read("https://ex.test/scan.png")
    assert r["content"] == "Recognised words" and r["metadata"]["doc_type"] == "image"


async def test_read_image_without_tesseract_is_structured(app_state, monkeypatch,
                                                          no_tesseract):
    from argus import server

    monkeypatch.setattr(server, "fetch", _served("image/jpeg", _png()))
    r = await server.read("https://ex.test/scan.jpg")
    assert (r["code"], r["detail"]) == ("unsupported_document", "ocr unavailable")


async def test_read_image_ocr_timeout_is_structured(app_state, monkeypatch):
    from argus import server

    monkeypatch.setattr(ocr, "tesseract", lambda: "tesseract")
    _fake_subprocess(monkeypatch, _slow)
    monkeypatch.setattr(server, "fetch", _served("image/png", _png()))
    r = await server.read("https://ex.test/scan.png")
    assert (r["code"], r["reason"]) == ("parse_failed", "timeout")


# --- scanned PDFs ------------------------------------------------------------------------


def _pdf(pages: list[str | None]) -> bytes:
    """A PDF whose ``None`` pages are scans: an image and no text layer."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text is None:
            page.insert_image(pymupdf.Rect(72, 72, 300, 200), stream=_png((200, 100)))
        else:
            page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_scanned_pdf_pages_are_ocred(fake_tesseract):
    fake_tesseract.text = "scanned page words"
    r = extract_pdf(_pdf(["Typed page with a text layer", None]), None, "text", 30)
    m = r["metadata"]
    assert "Typed page" in r["content"] and "scanned page words" in r["content"]
    assert m["ocr_pages"] == [2] and (m["ocr_pages_total"], m["ocr_pages_returned"]) == (1, 1)
    assert m["ocr"] == {"engine": "tesseract", "lang": "eng+ind"}
    png = fake_tesseract.calls[0][1]["input"]
    assert Image.open(io.BytesIO(png)).mode == "L"  # 150-dpi grayscale render


def test_scanned_pdf_fast_text_path_is_ocred_too(fake_tesseract, monkeypatch):
    from argus.extract import pdf

    monkeypatch.setattr(pdf, "_FAST_TEXT_MIN_PAGES", 1)
    r = extract_pdf(_pdf([None]), None, "text", 30)
    assert "## Page 1\n\nRecognised words" in r["content"]
    assert r["metadata"]["engine"] == "pymupdf-fast-text"


def test_scanned_pdf_ocr_page_cap_is_flagged(fake_tesseract, monkeypatch):
    monkeypatch.setattr(ocr, "MAX_OCR_PAGES", 1)
    r = extract_pdf(_pdf([None, None]), None, "text", 30)
    m = r["metadata"]
    assert (m["ocr_pages_total"], m["ocr_pages_returned"], m["ocr_pages"]) == (2, 1, [1])
    assert docs.from_pdf(r, "markdown")["truncated"] is True


def test_scanned_pdf_ocr_stops_at_the_documents_budget(fake_tesseract):
    r = extract_pdf(_pdf([None]), None, "text", 0.000001)
    assert (r["metadata"]["ocr_pages_total"], r["metadata"]["ocr_pages_returned"]) == (1, 0)
    assert not fake_tesseract.calls


def test_scanned_pdf_without_tesseract_keeps_todays_output(no_tesseract):
    r = extract_pdf(_pdf(["text page", None]), None, "text", 30)
    assert r["metadata"]["ocr"] == "unavailable" and "text page" in r["content"]
    assert "ocr_pages" not in r["metadata"]


def test_pdf_without_ocr_budget_or_without_scans_never_calls_tesseract(fake_tesseract):
    extract_pdf(_pdf([None]), None, "text")  # default: no OCR (read_pdf tables, quality)
    r = extract_pdf(_pdf(["only typed text here"]), None, "text", 30)
    assert not fake_tesseract.calls and "ocr" not in r["metadata"]


async def test_read_pdf_tool_ocrs_scanned_pages_in_text_mode(app_state, monkeypatch,
                                                             fake_tesseract):
    from argus import server

    async def fake_bytes(url, **kw):
        return url, _pdf([None]), "application/pdf"

    monkeypatch.setattr(server, "fetch_bytes", fake_bytes)
    r = await server.read_pdf("https://ex.test/scan.pdf")
    assert "Recognised words" in r["content"] and r["metadata"]["ocr_pages"] == [1]
    t = await server.read_pdf("https://ex.test/scan.pdf", mode="tables")
    assert "ocr_pages" not in t["metadata"]


async def test_read_scanned_pdf_url(app_state, monkeypatch, fake_tesseract):
    from argus import server

    monkeypatch.setattr(server, "fetch", _served("application/pdf", _pdf([None])))
    r = await server.read("https://ex.test/scan.pdf")
    assert "Recognised words" in r["content"] and r["metadata"]["ocr_pages"] == [1]


# --- security review of OCR (H1-L6), each fast ------------------------------------------


def test_tesseract_gets_the_jobs_remaining_time_minus_a_margin(fake_tesseract, monkeypatch):
    """H1: prep + a fixed 25 s could outlive the pool's kill and orphan tesseract."""
    import time

    from argus.extract import pool

    monkeypatch.setattr(pool, "_job_deadline", time.monotonic() + 10)
    ocr.ocr_png(b"png", ocr.OCR_TIMEOUT_S)
    assert fake_tesseract.calls[-1][1]["timeout"] <= 10 - ocr._KILL_MARGIN_S
    monkeypatch.setattr(pool, "_job_deadline", time.monotonic() + ocr._KILL_MARGIN_S)
    with pytest.raises(TimeoutError):  # no time left: tesseract is not even started
        ocr.ocr_png(b"png", ocr.OCR_TIMEOUT_S)
    assert len(fake_tesseract.calls) == 1


async def test_run_document_hands_the_job_its_deadline():
    import time

    from argus.extract import pool

    t0 = time.monotonic()
    got = await pool.run_document(pool.job_deadline, timeout=7)  # thread mode in tests
    assert t0 + 7 <= got <= time.monotonic() + 7 and pool.job_deadline() is None


def test_tesseract_runs_with_a_minimal_env_and_child_limits(fake_tesseract, monkeypatch):
    """M4: no server secrets in tesseract's env, one OpenMP thread; H1/M4 limits on POSIX."""
    import os

    monkeypatch.setenv("ARGUS_TOKEN_FOR_TEST", "secret")
    ocr.ocr_png(b"png", 5)
    kw = fake_tesseract.calls[-1][1]
    assert kw["env"]["OMP_THREAD_LIMIT"] == "1" and "ARGUS_TOKEN_FOR_TEST" not in kw["env"]
    assert set(kw["env"]) <= {"PATH", "OMP_THREAD_LIMIT", "TESSDATA_PREFIX", "SYSTEMROOT"}
    assert (kw.get("preexec_fn") is not None) == (os.name == "posix")


def _orphan_job(pid_file: str) -> None:
    """Runs in a document worker: a stand-in tesseract started like the real one, then the
    worker hangs until the pool kills it."""
    import time

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             **ocr._child_kwargs())
    with open(pid_file, "w") as f:
        f.write(str(child.pid))
    time.sleep(60)


def _alive(pid: int) -> bool:
    """Running, not a zombie: an orphan reaped late by PID 1 is already dead."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux-only")
async def test_tesseract_child_dies_with_its_killed_worker(tmp_path, monkeypatch):
    import asyncio
    import os

    from argus.extract import pool

    monkeypatch.setenv("ARGUS_EXTRACT_WORKERS", "1")
    pool.shutdown()
    pid_file = tmp_path / "child.pid"
    try:
        with pytest.raises(TimeoutError):
            await pool.run_document(_orphan_job, str(pid_file), timeout=8)
        pid = int(pid_file.read_text())
        for _ in range(50):
            if not _alive(pid):
                break
            await asyncio.sleep(0.1)
        else:
            os.kill(pid, 9)
            pytest.fail("the stand-in tesseract outlived its killed worker")
    finally:
        pool.shutdown()


def _tiff(ifds: int) -> bytes:
    """A 1x1 TIFF whose IFD chain is ``ifds`` long (each IFD a copy of the first)."""
    import struct

    buf = io.BytesIO()
    Image.new("L", (1, 1), 0).save(buf, "TIFF")
    t = bytearray(buf.getvalue())
    off = struct.unpack("<I", t[4:8])[0]
    n = struct.unpack("<H", t[off:off + 2])[0]
    ifd, base = bytes(t[off:off + 2 + 12 * n]), len(t)
    size = len(ifd) + 4
    t[off + 2 + 12 * n:off + 6 + 12 * n] = struct.pack("<I", base)
    tail = bytearray()
    for i in range(ifds - 1):
        tail += ifd + struct.pack("<I", base + (i + 1) * size if i < ifds - 2 else 0)
    return bytes(t + tail)


def test_tiff_frame_chain_is_probed_not_counted():
    """H2: n_frames walked 30k IFDs quadratically (3.3 MB: 12 s, 120k: 170 s)."""
    import time

    meta, t0 = {}, time.monotonic()
    ocr.image_png(_tiff(30_000), meta)
    assert time.monotonic() - t0 < 1
    assert (meta["frames_total"], meta["frames_returned"]) == (ocr.MAX_FRAMES, 1)
    assert meta["frames_total_at_least"] is True
    small = {}
    ocr.image_png(_tiff(3), small)
    assert small["frames_total"] == 3 and "frames_total_at_least" not in small


def test_only_allowlisted_image_formats_are_opened(fake_tesseract):
    """M3: an EPS served as image/png reached Pillow's Ghostscript plugin."""
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    with pytest.raises(DocError) as e:
        convert("image", eps, "https://ex.test/i.png", "markdown")
    assert e.value.reason == "unsupported_document" and not fake_tesseract.calls


def test_large_image_is_downscaled_before_ocr():
    """M4: a 36 MP scan went to tesseract whole; it gets at most 12 MP, same aspect."""
    png = ocr.image_png(_png((5000, 3000), mode="L"), {})
    with Image.open(io.BytesIO(png)) as im:
        assert im.width * im.height <= ocr.OCR_MAX_PIXELS
        assert abs(im.width / im.height - 5000 / 3000) < 0.01


def test_huge_pdf_page_is_skipped_not_fatal(fake_tesseract):
    """L5: a 2,000,000-pt MediaBox raised FzErrorLimit out of get_pixmap: the read failed."""
    doc = pymupdf.open(stream=_pdf([None, None]), filetype="pdf")
    doc[0].set_mediabox(pymupdf.Rect(0, 0, 2_000_000, 2_000_000))
    meta = {}
    done = ocr.ocr_pdf_pages(doc, [0, 1], 30, meta)
    assert list(done) == [1] and (meta["ocr_pages_total"], meta["ocr_pages_returned"]) == (2, 1)


def test_bmp_magic_needs_a_real_header():
    """L6: any body starting with b'BM' was treated as an image."""
    buf = io.BytesIO()
    Image.new("L", (4, 4)).save(buf, "BMP")
    assert sniff("application/octet-stream", buf.getvalue(), "", "https://ex.test/d") == "image"
    for junk in (b"BMW owners manual" + bytes(40), b"BM" + bytes(40)):
        assert sniff("application/octet-stream", junk, "", "https://ex.test/d") is None
