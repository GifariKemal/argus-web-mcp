"""httpx static fast-path with explicit per-hop SSRF re-validation.

Redirects are followed manually (the safe client has follow_redirects=False) so
every hop is re-guarded - closing the open-redirect-to-internal SSRF hole.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from ..security.ssrf import aresolve_and_validate, validate_url

_DEFAULT_PORTS = {"http": 80, "https": 443}
_DEFAULT_UA = "ArgusBot/0.1 (+https://suriota.com; self-hosted research)"

# DoS guard for the shared box. Two layers: a Content-Length header fast-path
# (reject before reading a byte) AND a streaming hard-cap that aborts a chunked /
# no-length body the moment the accumulated bytes exceed the limit.
MAX_FETCH_BYTES = 32 * 1024 * 1024


def _check_size(resp: httpx.Response) -> None:
    cl = resp.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_FETCH_BYTES:
        raise FetchError("fetch_failed", f"response too large: {cl} bytes")


class FetchError(Exception):
    """Non-SSRF fetch failure surfaced to the fetch orchestrator."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def _guard(url: str) -> None:
    """Scheme allowlist + resolve-then-validate for a single hop. Raises SSRFError."""
    validate_url(url)
    parts = urlsplit(url)
    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    # ponytail: re-resolves here AND in the safe transport (defence in depth); OS
    # caches DNS so the double lookup is cheap. Makes per-hop blocking explicit/testable.
    # Resolver runs off the loop (aresolve) so a slow lookup per hop can't stall the worker.
    await aresolve_and_validate(parts.hostname, port)


async def _stream_capped(resp: httpx.Response) -> bytes:
    """Read the body via streaming, aborting once it exceeds ``MAX_FETCH_BYTES``."""
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > MAX_FETCH_BYTES:
            raise FetchError("fetch_failed", "response too large (streamed cap)")
    return bytes(buf)


async def _get_guarded(
    url: str, *, client: httpx.AsyncClient, timeout: float, max_redirects: int
) -> tuple[httpx.Response, bytes]:
    """GET ``url`` following redirects manually, re-guarding each hop.

    Returns the final ``(response, body_bytes)``. The body is read via a streamed,
    hard-capped accumulation so a chunked / no-Content-Length response cannot
    balloon memory. Redirect hops never read their body.
    """
    current = url
    headers = {"user-agent": _DEFAULT_UA}
    for _ in range(max_redirects + 1):
        await _guard(current)
        try:
            async with client.stream(
                "GET", current, timeout=timeout, headers=headers
            ) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    # Do NOT read the body on a redirect hop; re-guard the target.
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    continue
                _check_size(resp)  # header fast-path
                body = await _stream_capped(resp)  # streaming hard-cap
                return resp, body
        except httpx.HTTPError as exc:
            raise FetchError("fetch_failed", f"{type(exc).__name__}: {exc}") from exc
    raise FetchError("fetch_failed", f"exceeded {max_redirects} redirects")


async def fetch_static(
    url: str, *, client: httpx.AsyncClient, timeout: float = 30, max_redirects: int = 5
) -> dict:
    """GET ``url`` (guarded redirects). Returns ``{final_url, status, html, render_path}``."""
    resp, body = await _get_guarded(
        url, client=client, timeout=timeout, max_redirects=max_redirects
    )
    # Anti-bot status blocks (Cloudflare/DataDome/WAF: 403/429/503) return a challenge page,
    # not content. Raise FetchError so fetch.core escalates to the stealth-browser + Wayback
    # ladder (that ladder is gated on `except FetchError` and previously NEVER fired on a
    # status block - a challenge page was returned as if it were real content).
    if resp.status_code in (403, 429, 503):
        raise FetchError("blocked_by_antibot", f"status {resp.status_code} (anti-bot block)")
    html = body.decode(resp.encoding or "utf-8", errors="replace")
    return {
        "final_url": str(resp.url),
        "status": resp.status_code,
        "html": html,
        "render_path": "static",
    }


async def fetch_bytes(
    url: str, *, client: httpx.AsyncClient, timeout: float = 60, max_redirects: int = 5
) -> tuple[str, bytes, str]:
    """GET ``url`` (guarded redirects) as bytes. Returns ``(final_url, content, ctype)``."""
    resp, body = await _get_guarded(
        url, client=client, timeout=timeout, max_redirects=max_redirects
    )
    return str(resp.url), body, resp.headers.get("content-type", "")
