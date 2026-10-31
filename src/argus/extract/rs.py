"""rs-trafilatura (Rust port of trafilatura, MIT OR Apache-2.0) as a subprocess.

Its two embedded XGBoost models classify the page type and score the extraction. On the
WCXB benchmark (2026-10-31) its text beats Argus's trafilatura path on forum, collection,
listing, service and product pages (dev F1 0.847 vs 0.817 overall), and a confidence below
0.6 marks pages averaging F1 0.33. ``article.extract_article`` routes on the page type and
reports the confidence. The image builds it from a
pinned commit (deploy/rs-trafilatura/); anywhere else it is optional: without the binary
every read is exactly what Argus did before.

Runs inside the extraction worker (``pool.extract_async``), so the ~50 ms spawn never
blocks the event loop. Fixed argv, no shell, minimal environment; only the HTML is
untrusted and it goes to stdin. ``ARGUS_RS_TRAFILATURA`` = a path, or ``off``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)
TIMEOUT_S = 10  # p95 is 0.17 s a page; the article worker's own kill time is 30 s
# = fetch.static.MAX_FETCH_BYTES (a test pins it); not imported, so workers load only
# the extract package. Bigger input skips rs (Argus's own path reads it), never cuts it.
MAX_INPUT_BYTES = 32 * 1024 * 1024
_ENV_KEYS = ("SYSTEMROOT",)  # Windows needs it; the Linux binary needs nothing


@functools.cache
def binary() -> str | None:
    """The executable, or None when it is absent or ``ARGUS_RS_TRAFILATURA=off``."""
    path = os.environ.get("ARGUS_RS_TRAFILATURA", "").strip()
    if path.lower() == "off":
        return None
    return shutil.which(path or "rs-trafilatura")


def extract(html: str, url: str) -> dict | None:
    """``{"page_type", "confidence", "text"}`` from rs-trafilatura, or None when it is
    absent, fails, times out, prints something unexpected or finds no text."""
    exe = binary()
    data = html.encode("utf-8", "replace")
    if exe is None or len(data) > MAX_INPUT_BYTES:
        return None
    # Spawned like tesseract: minimal env and, on Linux, it dies with its worker and has an
    # address-space cap, so a pool kill caused by another page cannot orphan it.
    from . import ocr  # lazy: ocr -> pool -> article -> rs would be a cycle at import

    kwargs = ocr._child_kwargs()
    kwargs["env"] = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    try:
        proc = subprocess.run([exe, *(["--url", url] if url else [])], input=data,  # noqa: S603
                              capture_output=True, timeout=TIMEOUT_S, check=False, **kwargs)
        if proc.returncode != 0:
            return None
        out = json.loads(proc.stdout)
        text, conf = out["main_content"], out["confidence"]
        if not isinstance(text, str) or not isinstance(conf, (int, float)):
            return None
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as exc:
        logger.info("rs-trafilatura skipped: %s", type(exc).__name__)
        return None
    if not text.strip():
        return None
    page_type = out.get("page_type")
    return {"page_type": page_type if isinstance(page_type, str) else None,
            "confidence": float(conf), "text": text}
