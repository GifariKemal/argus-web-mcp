"""httpx static fast-path with explicit per-hop SSRF re-validation.

Redirects are followed manually (the safe client has follow_redirects=False) so
every hop is re-guarded - closing the open-redirect-to-internal SSRF hole.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import httpx
import lxml.etree
import lxml.html

from ..config import egress_proxy_for
from ..models import record_stage
from ..security.ssrf import aresolve_and_validate, build_safe_async_client, validate_url

_DEFAULT_PORTS = {"http": 80, "https": 443}
_DEFAULT_UA = "ArgusBot/0.1 (+https://suriota.com; self-hosted research)"
# No brotli/zstd: httpx inflates each raw chunk before the streaming cap sees it, and
# brotli packs 200 MB of zeros into 329 bytes (measured), so one chunk could take the
# container past its memory limit. gzip/deflate stay within ~1000:1 per 64 KB chunk.
_SAFE_ENCODINGS = frozenset({"", "identity", "gzip", "deflate"})
_DEFAULT_HEADERS = {"user-agent": _DEFAULT_UA, "accept-encoding": "gzip, deflate"}

# --- anti-bot wall detection (shared by the static and browser tiers) ----------------
# Markers of an anti-bot interstitial (Cloudflare / Akamai / PerimeterX challenge pages).
_BLOCK_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "attention required",
    "access denied",
    "enable javascript and cookies",
    "verify you are human",
    "complete the following challenge",  # DuckDuckGo lite/html CAPTCHA (seen 2026-10-31)
)
_BLOCK_STATUSES = {403, 429, 503}
# crawl4ai verdicts that mean "thin page", not "wall": a tiny legit page or JS shell is the
# thin-escalation path's business, and calling it blocked would fail a real read.
_THIN_REASONS = ("Structural:", "Near-empty")
_CHALLENGE_TEXT_MAX = 3000  # visible chars; challenge pages carry a few hundred at most
_VISIBLE_SCAN_CHARS = 200_000
_TAG_FALLBACK = re.compile(r"<[^<>]*>")  # linear: never scans past the next '<'
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")  # lxml refuses str input that declares one


def visible_text_len(html: str) -> int:
    """Visible-text char count (script/style/tags removed) on the first 200k chars, linear
    in page size via lxml. The regex forms this replaces were quadratic on hostile markup
    (11 s per 100 KB of ``<``, measured) and ran on the event loop for every static fetch."""
    html = _XML_DECL.sub("", (html or "")[:_VISIBLE_SCAN_CHARS], count=1)
    if not html.strip():
        return 0
    try:
        doc = lxml.html.document_fromstring(html)
    except (lxml.etree.ParserError, ValueError):  # empty document, control characters
        return len(" ".join(_TAG_FALLBACK.sub(" ", html).split()))
    for el in list(doc.iter("script", "style")):
        el.drop_tree()
    return len(" ".join(doc.text_content().split()))


def _looks_blocked(html: str, status: int | None) -> bool:
    """Heuristic: does this response look like an anti-bot challenge rather than content?

    crawl4ai's detector supplies vendor fingerprints at any size (DataDome
    captcha-delivery, PerimeterX, Incapsula, Kasada, Akamai reference ids, Cloudflare
    challenge scripts) and generic phrases on error statuses; our markers add the
    Cloudflare interstitial text a 200 challenge carries, on short pages only.
    """
    if status in _BLOCK_STATUSES:
        return True
    from crawl4ai.antibot_detector import is_blocked  # lazy: crawl4ai costs ~1 s to import

    html = (html or "")[:_VISIBLE_SCAN_CHARS]  # interstitials are small; bounds the scan
    blocked, reason = is_blocked(status, html)
    # On a 2xx, a fingerprint/phrase hit must come with a thin page: an article about bot
    # walls quotes "Pardon Our Interruption" or "Reference #" and is content, while a real
    # interstitial carries almost no visible text. Error statuses need no corroboration.
    vlen = visible_text_len(html)
    thin = (status or 200) >= 400 or vlen < _CHALLENGE_TEXT_MAX
    if blocked and thin and not reason.startswith(_THIN_REASONS):
        return True
    # Markers can be class names, so scan the markup (bounded above), not the visible text;
    # inline JS pushes them past any fixed prefix, and the thin gate keeps a real article
    # that quotes "access denied" from counting as a wall. Script bodies are skipped: a
    # thin SPA shell ships i18n strings like "Access denied" in its bundle.
    if vlen >= _CHALLENGE_TEXT_MAX:
        return False
    head, *rest = html.lower().split("<script")  # linear, unlike a lazy <script>.*? regex
    low = head + " ".join(part.partition("</script>")[2] for part in rest)
    return any(m in low for m in _BLOCK_MARKERS)


def _challenge_headers(resp: httpx.Response) -> bool:
    """A 2xx that is really a wall: Cloudflare marks managed challenges with
    ``cf-mitigated: challenge``; AWS WAF answers its CAPTCHA/challenge actions (often an
    empty 202) with ``x-amzn-waf-action``."""
    return (resp.headers.get("cf-mitigated", "").lower() == "challenge"
            or "x-amzn-waf-action" in resp.headers)


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
    url: str, *, client: httpx.AsyncClient, timeout: float, max_redirects: int,
    headers: dict | None = None,
) -> tuple[httpx.Response, bytes]:
    """GET ``url`` following redirects manually, re-guarding each hop.

    Returns the final ``(response, body_bytes)``. The body is read via a streamed,
    hard-capped accumulation so a chunked / no-Content-Length response cannot
    balloon memory. Redirect hops never read their body.
    """
    current = url
    # Cookies live for this one call: a set-cookie-then-302 wall (DataDome, consent pages)
    # still completes, but nothing carries over to the next caller of a shared client.
    jar = httpx.Cookies()

    async def _hop(c: httpx.AsyncClient, t) -> tuple[httpx.Response, bytes | None]:
        req = c.build_request("GET", current, timeout=t, headers={**_DEFAULT_HEADERS,
                                                                    **(headers or {})})
        jar.set_cookie_header(req)
        resp = await c.send(req, stream=True)
        try:
            jar.extract_cookies(resp)
            if resp.is_redirect and "location" in resp.headers:
                return resp, None  # Do NOT read the body on a redirect hop.
            _check_size(resp)  # header fast-path
            enc = resp.headers.get("content-encoding", "").lower().strip()
            if enc not in _SAFE_ENCODINGS:  # we never ask for it; a server insisting is hostile
                raise FetchError("fetch_failed", f"unsupported content-encoding {enc!r}")
            # httpx timeouts are per socket operation; a trickling body passes every one of
            # them, so the whole read gets the hop's budget as a wall clock.
            return resp, await asyncio.wait_for(_stream_capped(resp), timeout)
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
        except TimeoutError as exc:
            raise FetchError("fetch_failed", f"body read exceeded {timeout:g}s") from exc
        if body is None:
            try:
                current = str(httpx.URL(current).join(resp.headers["location"]))  # re-guarded
            except (httpx.InvalidURL, ValueError) as exc:  # hostile Location (bad port...)
                raise FetchError("fetch_failed", "unparseable redirect location") from exc
            continue
        return resp, body
    raise FetchError("fetch_failed", f"exceeded {max_redirects} redirects",
                     status=resp.status_code)


async def fetch_static(
    url: str, *, client: httpx.AsyncClient, timeout: float = 30, max_redirects: int = 5,
    headers: dict | None = None, api: bool = False,
) -> dict:
    """GET ``url`` (guarded redirects). Returns ``{final_url, status, html, render_path}``.
    ``headers`` are added to every hop, so a caller sending credentials passes
    ``max_redirects=0``: a redirect then fails instead of carrying them to another host.
    ``api=True`` (a known JSON/feed endpoint) skips the challenge-page body heuristic, which
    would read a short answer quoting "access denied" in user text as a wall."""
    resp, body = await _get_guarded(
        url, client=client, timeout=timeout, max_redirects=max_redirects, headers=headers
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
    # A 2xx can be a wall too: Cloudflare managed challenges and AWS WAF say so in a
    # header; the body check below catches the ones that do not.
    if resp.status_code < 300 and _challenge_headers(resp):
        raise FetchError(
            "blocked_by_antibot", f"status {resp.status_code} (challenge headers)",
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
    if resp.status_code < 300 and not api and _looks_blocked(html, resp.status_code):
        raise FetchError(
            "blocked_by_antibot", f"status {resp.status_code} (challenge page)",
            status=resp.status_code,
        )
    res = {
        "final_url": str(resp.url),
        "status": resp.status_code,
        "html": html,
        "render_path": "static",
    }
    if headers:  # a credentialed caller may need its session cookies rotated (X ct0)
        res["set_cookie"] = resp.headers.get_list("set-cookie")
    return res


async def fetch_bytes(
    url: str, *, client: httpx.AsyncClient, timeout: float = 60, max_redirects: int = 5
) -> tuple[str, bytes, str]:
    """GET ``url`` (guarded redirects) as bytes. Returns ``(final_url, content, ctype)``."""
    resp, body = await _get_guarded(
        url, client=client, timeout=timeout, max_redirects=max_redirects
    )
    return str(resp.url), body, resp.headers.get("content-type", "")
