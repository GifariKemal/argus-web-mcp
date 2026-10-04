"""httpx static fast-path with explicit per-hop SSRF re-validation.

Redirects are followed manually (the safe client has follow_redirects=False) so
every hop is re-guarded - closing the open-redirect-to-internal SSRF hole.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import httpx

from ..config import egress_proxy_for
from ..models import record_stage
from ..security.ssrf import aresolve_and_validate, build_safe_async_client, validate_url

_DEFAULT_PORTS = {"http": 80, "https": 443}
_DEFAULT_UA = "ArgusBot/0.1 (+https://suriota.com; self-hosted research)"

# Sniff an HTML/XML meta-declared charset from the head of the body when the HTTP header
# carried none - httpx defaults to utf-8, silently mojibake-ing legacy CJK/Cyrillic pages.
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+?charset=["']?\s*([\w.:-]+)|<\?xml[^>]+?encoding=["']([\w.:-]+)""",
    re.IGNORECASE,
)


def _sniff_meta_charset(body: bytes) -> str | None:
    m = _META_CHARSET_RE.search(body[:2048])
    if not m:
        return None
    enc = (m.group(1) or m.group(2) or b"").decode("ascii", "ignore").strip()
    return enc or None

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

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        self.code = code
        self.status = status  # HTTP status when the failure was a status answer
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


# A pooled client belongs to the event loop that opened its connections, so the proxied
# client is kept per loop (production runs one; tests and scripts run many).
_proxied: dict[tuple[asyncio.AbstractEventLoop, str], httpx.AsyncClient] = {}
# Bounds the CONNECT handshake so a hung proxy leaves time for the direct fallback.
PROXY_CONNECT_TIMEOUT = 8.0


def _proxied_client(via: str) -> httpx.AsyncClient:
    key = (asyncio.get_running_loop(), via)
    if key not in _proxied:
        _proxied.clear()  # ponytail: drops stale clients unclosed; one loop + one proxy in prod
        _proxied[key] = build_safe_async_client(via_proxy=via)
    return _proxied[key]


def _egress_client_for(host: str) -> httpx.AsyncClient | None:
    """The proxied client when ``host`` is listed in ARGUS_EGRESS_PROXY_HOSTS, else None.
    Same SSRF-pinned client, only tunnelled (see security.ssrf.connect_tunnel)."""
    via = egress_proxy_for(host)
    return None if via is None else _proxied_client(via)


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
    # Cookies live for this one call: a set-cookie-then-302 wall (DataDome, consent pages)
    # still completes, but nothing carries over to the next caller of a shared client.
    jar = httpx.Cookies()

    async def _hop(c: httpx.AsyncClient, t) -> tuple[httpx.Response, bytes | None]:
        req = c.build_request("GET", current, timeout=t, headers=headers)
        jar.set_cookie_header(req)
        resp = await c.send(req, stream=True)
        try:
            jar.extract_cookies(resp)
            if resp.is_redirect and "location" in resp.headers:
                return resp, None  # Do NOT read the body on a redirect hop.
            _check_size(resp)  # header fast-path
            return resp, await _stream_capped(resp)  # streaming hard-cap
        finally:
            await resp.aclose()

    for _ in range(max_redirects + 1):
        await _guard(current)
        proxied = _egress_client_for(httpx.URL(current).host)
        try:
            if proxied is None:
                resp, body = await _hop(client, timeout)
            else:
                record_stage("fetch.egress_proxy")
                try:
                    resp, body = await _hop(proxied, httpx.Timeout(
                        timeout, connect=min(timeout, PROXY_CONNECT_TIMEOUT)))
                except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout):
                    # Proxy down, refusing or hung (connect_tunnel turns every handshake
                    # failure into ProxyError), or TLS through the tunnel failing: the
                    # direct path is no worse than before, and it re-guards.
                    record_stage("fetch.egress_proxy_fail")
                    resp, body = await _hop(client, timeout)
        except httpx.HTTPError as exc:
            raise FetchError("fetch_failed", f"{type(exc).__name__}: {exc}") from exc
        if body is None:
            current = str(httpx.URL(current).join(resp.headers["location"]))  # re-guarded
            continue
        return resp, body
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
    # DataDome walls answer 401 with x-datadome (Reuters, WSJ, measured 2026-10-04). Other
    # 401s (API auth, Basic auth) are an honest answer and stay content.
    datadome_401 = resp.status_code == 401 and "x-datadome" in resp.headers
    if resp.status_code in (403, 429, 503) or datadome_401:
        raise FetchError(
            "blocked_by_antibot", f"status {resp.status_code} (anti-bot block)",
            status=resp.status_code,
        )
    # Any other 5xx (500/502/504, Cloudflare 520-526) is an error page, not content: send
    # it down the same ladder instead of extracting "Web server is returning an unknown error".
    if resp.status_code >= 500:
        raise FetchError(
            "fetch_failed", f"status {resp.status_code} (server error)", status=resp.status_code
        )
    # Decode with the HEADER-declared charset when present; otherwise sniff a meta-declared
    # one before falling back to utf-8 (httpx's resp.encoding defaults to utf-8 with no header,
    # which would corrupt meta-only legacy encodings past any downstream re-decode).
    enc = resp.charset_encoding or _sniff_meta_charset(body) or "utf-8"
    try:
        html = body.decode(enc, errors="replace")
    except LookupError:  # bogus/unknown meta-declared encoding name
        html = body.decode("utf-8", errors="replace")
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
