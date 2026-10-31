"""OCR via the tesseract binary: ``read`` of an image URL, and scanned PDF pages.

Only ever called from the document pool (``pool.run_document``) or the PDF thread, so a
hostile image or a stuck tesseract costs a worker, never the event loop. Measured on the
production server (tesseract 5.3.4, ``eng+ind``): word recall 0.988 against the text layer
of 7 rendered PDF pages, 1.5-6 s a page on 2 CPUs. The binary is optional: without it an
image read is ``unsupported_document`` and a scanned page stays empty, as before.

Tesseract is bounded three ways: its timeout ends before the job's kill time
(``pool.job_deadline``), on Linux it dies with its worker (``PR_SET_PDEATHSIG``) and has an
address-space limit, and it gets a minimal environment with one OpenMP thread.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
import time

import pymupdf
from PIL import Image, UnidentifiedImageError

from . import pool

logger = logging.getLogger(__name__)
OCR_LANG = "eng+ind"
OCR_META = {"engine": "tesseract", "lang": OCR_LANG}
# Per image or page (a PDF also has its whole-document budget); always cut to end
# _KILL_MARGIN_S before the job's kill time, so a worker is never killed under tesseract.
OCR_TIMEOUT_S = 25
_KILL_MARGIN_S = 3
MAX_PIXELS = 40_000_000  # checked on the header, before anything is decoded
OCR_MAX_PIXELS = 12_000_000  # what tesseract gets: bigger scans are downscaled first
MAX_FRAMES = 16  # frames probed; n_frames walks every TIFF IFD (30k IFDs: 12 s)
MAX_OCR_PAGES = 20  # per PDF: 20 pages x 1.5-6 s already fills a read's budget
BUDGET_SHARE = 0.75  # of a PDF read's timeout spent on OCR; the rest is the text layer
_SCAN_TEXT_BELOW = 20  # a page with fewer text-layer chars than this is a scan
_PDF_DPI = 150
# EPS, PSD, ICO and the rest stay closed: Pillow's EPS plugin runs Ghostscript.
_FORMATS = ("PNG", "JPEG", "GIF", "TIFF", "BMP", "WEBP")
_ENV_KEYS = ("PATH", "TESSDATA_PREFIX", "SYSTEMROOT")  # SYSTEMROOT: Windows needs it
_CHILD_AS_BYTES = 1536 * 1024 * 1024  # tesseract's address space (Linux)


class Refused(ValueError):
    """An image refused before OCR; ``docs`` turns it into a DocError with ``reason``."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(reason, detail)  # args round-trip through the worker's pickle
        self.reason, self.detail = reason, detail


def tesseract() -> str | None:
    return shutil.which("tesseract")


def _limit_child(parent: int) -> None:  # pragma: no cover - runs in the forked child
    """preexec_fn on Linux: die with the worker (a pool kill must not orphan tesseract)
    and cap the address space."""
    import ctypes
    import resource
    import signal

    ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    if os.getppid() != parent:  # the worker died before prctl took effect
        os._exit(1)
    resource.setrlimit(resource.RLIMIT_AS, (_CHILD_AS_BYTES, _CHILD_AS_BYTES))


def _child_kwargs() -> dict:
    """How tesseract (or anything this module starts) is spawned: a minimal environment,
    one OpenMP thread, and on Linux the parent-death signal and memory cap."""
    env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    env["OMP_THREAD_LIMIT"] = "1"  # two document workers on two CPUs: no oversubscription
    if sys.platform != "linux":
        return {"env": env}
    parent = os.getpid()
    return {"env": env, "preexec_fn": lambda: _limit_child(parent)}


def ocr_png(png: bytes, timeout: float) -> str:
    """Text of one PNG. TimeoutError past ``timeout`` or too close to the job's kill time
    (subprocess.run kills tesseract), RuntimeError when it fails, FileNotFoundError when it
    is not installed."""
    exe = tesseract()
    if exe is None:
        raise FileNotFoundError("tesseract")
    if (deadline := pool.job_deadline()) is not None:
        timeout = min(timeout, deadline - time.monotonic() - _KILL_MARGIN_S)
    if timeout < 1:
        raise TimeoutError("no time left for ocr in this job")
    try:
        # Fixed argv, no shell: only the image bytes are untrusted, and they go to stdin.
        proc = subprocess.run([exe, "stdin", "stdout", "-l", OCR_LANG], input=png,  # noqa: S603
                              capture_output=True, timeout=timeout, check=False,
                              **_child_kwargs())
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"ocr exceeded {timeout:g}s") from exc
    if proc.returncode != 0:  # stderr is tesseract's, not ours: logged by the caller's type
        raise RuntimeError(f"tesseract exit {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace").strip()


def image_png(data: bytes, meta: dict) -> bytes:
    """Grayscale PNG (at most OCR_MAX_PIXELS) of an image's first frame, for tesseract.
    Pillow opens only allowlisted formats and reads the header first: past MAX_PIXELS it
    raises Refused before decoding a pixel. Frames are probed up to MAX_FRAMES and go to
    ``frames_total``/``frames_returned`` (``frames_total_at_least`` when there are more)."""
    try:
        im = Image.open(io.BytesIO(data), formats=_FORMATS)  # lazy: the header only
    except UnidentifiedImageError:
        raise Refused("unsupported_document", "not a PNG/JPEG/GIF/TIFF/BMP/WEBP image") \
            from None
    with im:
        if im.width * im.height > MAX_PIXELS:
            raise Refused("doc_too_large",
                          f"{im.width}x{im.height} image is over {MAX_PIXELS} pixels")
        frames = _frames(im)
        if frames > 1:
            meta.update(frames_total=frames, frames_returned=1)
            if frames == MAX_FRAMES:
                meta["frames_total_at_least"] = True
        im.seek(0)
        scale = min(1.0, (OCR_MAX_PIXELS / (im.width * im.height)) ** 0.5)
        size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
        im.draft("L", size)  # JPEG decodes straight at a reduced size; others ignore it
        gray = im.convert("L")
        if gray.width * gray.height > OCR_MAX_PIXELS:
            gray = gray.resize(size)
        buf = io.BytesIO()
        gray.save(buf, "PNG")
    return buf.getvalue()


def _frames(im: Image.Image) -> int:
    """Frames, counted by seeking at most MAX_FRAMES deep (never ``n_frames``)."""
    count = 1
    while count < MAX_FRAMES:
        try:
            im.seek(count)
        except EOFError:
            break
        count += 1
    return count


def ocr_pdf_pages(doc: pymupdf.Document, page_indices: list[int], seconds: float,
                  meta: dict) -> dict[int, str]:
    """OCR the scanned pages (an image and next to no text layer) among ``page_indices``
    within ``seconds`` overall: ``{page index: text}``. Counts go to ``ocr_pages_total`` /
    ``ocr_pages_returned`` (past MAX_OCR_PAGES, the budget, or a page that cannot be
    rendered they differ), the pages done to ``ocr_pages``; without tesseract
    ``meta['ocr'] = 'unavailable'`` and nothing changes."""
    scans = [i for i in page_indices if doc[i].get_images()
             and len(doc[i].get_text().strip()) < _SCAN_TEXT_BELOW]
    if not scans:
        return {}
    if tesseract() is None:
        meta["ocr"] = "unavailable"
        return {}
    deadline, done = time.monotonic() + seconds, {}
    for i in scans[:MAX_OCR_PAGES]:
        left = min(OCR_TIMEOUT_S, deadline - time.monotonic())
        if left < 1:
            break
        try:
            page = doc[i]
            # A huge page at 150 dpi would be a pixel bomb: lower the dpi to fit.
            area = max(page.rect.width * page.rect.height, 1)
            dpi = max(10, min(_PDF_DPI, int(72 * (OCR_MAX_PIXELS / area) ** 0.5)))
            png = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY).tobytes("png")
            done[i] = ocr_png(png, left)
        except Exception as exc:  # noqa: BLE001 - one page (MuPDF's size limit, a failed
            # or slow tesseract) is skipped and counted, never the whole read
            logger.info("ocr skipped page %d: %s", i + 1, type(exc).__name__)
    meta.update(ocr=OCR_META, ocr_pages=[i + 1 for i in done], ocr_pages_total=len(scans),
                ocr_pages_returned=len(done))
    return done
