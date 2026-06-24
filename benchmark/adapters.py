"""Benchmark adapters: uniform `async run(item) -> {content, latency, ok}`.

Three adapters:
  - argus            : the real in-process Argus tool (read / read_pdf), full tiered
                       fetch + browser escalation + SSRF guard + cache.
  - raw_trafilatura  : naive free baseline - httpx.get + trafilatura.extract (static, no JS).
  - readability_only : free baseline - httpx.get + readability-lxml Document.summary().

Paid adapters (Jina/Firecrawl/Exa/Tavily): documented stub, never called without a key.

The Argus adapter needs the module-global server._S set up. The caller owns the
lifecycle via setup_argus() / teardown_argus() (a real safe client + real browser).
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path

import httpx
import trafilatura
from readability import Document

# --- shared HTTP for the free baselines (own client, NOT the SSRF-guarded one) ---
_UA = "Mozilla/5.0 (compatible; ArgusBench/0.1; +https://suriota.com)"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()


def _http_get(url: str, timeout: float = 30.0) -> str | None:
    try:
        resp = httpx.get(
            url, follow_redirects=True, timeout=timeout, headers={"User-Agent": _UA}
        )
        resp.raise_for_status()
        return resp.text
    except Exception:  # noqa: BLE001 - baseline must degrade, never crash the run
        return None


# --- Argus (real in-process tool) -------------------------------------------

_ARGUS_TMP: tempfile.TemporaryDirectory | None = None


async def setup_argus() -> None:
    """Install a REAL Argus State (safe client + cache + live browser) into server._S."""
    global _ARGUS_TMP
    from argus import server
    from argus.cache import Cache
    from argus.fetch.render import BrowserPool
    from argus.security.ssrf import build_safe_async_client

    _ARGUS_TMP = tempfile.TemporaryDirectory(prefix="argus-bench-")
    base = Path(_ARGUS_TMP.name)
    browser = BrowserPool()
    await browser.start()
    server._S = server.State(
        client=build_safe_async_client(timeout=30),
        cache=Cache(db_path=str(base / "cache.db"), blob_dir=str(base / "blobs")),
        browser=browser,
    )


async def teardown_argus() -> None:
    from argus import server

    if server._S is not None:
        if server._S.browser is not None:
            await server._S.browser.stop()
        await server._S.client.aclose()
        server._S.cache.close()
        server._S = None
    global _ARGUS_TMP
    if _ARGUS_TMP is not None:
        _ARGUS_TMP.cleanup()
        _ARGUS_TMP = None


class ArgusAdapter:
    name = "argus"

    async def run(self, item: dict) -> dict:
        from argus import server

        url = item["url"]
        t0 = time.perf_counter()
        try:
            if item.get("category") == "pdf":
                res = await server.read_pdf(url)
            else:
                res = await server.read(url)
        except Exception as exc:  # noqa: BLE001 - tool shouldn't raise, but be safe
            return {"content": "", "latency": time.perf_counter() - t0, "ok": False,
                    "detail": f"exception: {exc}"}
        latency = time.perf_counter() - t0
        content = res.get("content", "") if isinstance(res, dict) else ""
        ok = bool(content) and not (isinstance(res, dict) and res.get("code"))
        out = {"content": content or "", "latency": latency, "ok": ok}
        if not ok and isinstance(res, dict):
            out["detail"] = res.get("code") or res.get("error") or "empty"
        return out


# --- raw_trafilatura (naive free static scraper) ----------------------------


class RawTrafilaturaAdapter:
    name = "raw_trafilatura"

    async def run(self, item: dict) -> dict:
        url = item["url"]
        t0 = time.perf_counter()
        html = _http_get(url)
        content = ""
        if html:
            try:
                content = trafilatura.extract(html) or ""
            except Exception:  # noqa: BLE001
                content = ""
        latency = time.perf_counter() - t0
        return {"content": content, "latency": latency, "ok": bool(content)}


# --- readability_only (free baseline) ---------------------------------------


class ReadabilityOnlyAdapter:
    name = "readability_only"

    async def run(self, item: dict) -> dict:
        url = item["url"]
        t0 = time.perf_counter()
        html = _http_get(url)
        content = ""
        if html:
            try:
                content = _strip_html(Document(html).summary())
            except Exception:  # noqa: BLE001
                content = ""
        latency = time.perf_counter() - t0
        return {"content": content, "latency": latency, "ok": bool(content)}


def free_adapters() -> list:
    """The baselines that never need an API key - always safe to run."""
    return [ArgusAdapter(), RawTrafilaturaAdapter(), ReadabilityOnlyAdapter()]


# Paid adapters are intentionally NOT implemented - they would cost money and need
# secrets. Register them here keyed by the env var that must be present; run_bench
# skips any whose key is unset. Left empty on purpose for P1.
KEYED_ADAPTERS: dict[str, object] = {}
