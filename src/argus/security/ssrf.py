"""SSRF trust boundary for Argus.

Follows the OWASP SSRF cheat sheet: scheme allowlist, resolve-then-validate,
deny private/metadata/reserved ranges, and anti-DNS-rebinding by pinning the
outgoing connection to a *validated* resolved IP while preserving the original
Host header and TLS SNI.

This is a HARD GATE. Keep it small, pure, and fully covered.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlsplit

import httpcore
import httpx

from ..config import DNS_TIMEOUT

ALLOWED_SCHEMES = {"http", "https"}

# Cloud instance-metadata endpoints (AWS/GCP/Azure). 169.254.169.254 is also
# caught by the link-local check, but we hard-block it explicitly for clarity
# and defence in depth. fd00:ec2::254 is the IMDS IPv6 address.
_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}

# RFC 6598 Carrier-Grade NAT range - not flagged is_private by ipaddress.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")

_DEFAULT_PORTS = {"http": 80, "https": 443}


class SSRFError(Exception):
    """Raised when a URL/host/IP fails the SSRF trust boundary."""

    code = "ssrf_blocked"


class DNSError(SSRFError):
    """The host did not resolve (NXDOMAIN, resolver error, timeout, no addresses).

    Not an SSRF decision, so it reports ``dns_failed``; it subclasses SSRFError so every
    existing ``except SSRFError`` still refuses the fetch and keeps it out of the fallbacks.
    """

    code = "dns_failed"


def validate_url(url: str) -> None:
    """Scheme allowlist + host present. Cheap pre-check; does NOT resolve DNS."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise SSRFError(f"scheme not allowed: {parts.scheme!r}")
    if not parts.hostname:
        raise SSRFError("missing host")


def is_blocked_ip(ip: str) -> bool:
    """True if ``ip`` is unsafe to connect to (pure function)."""
    if ip in _METADATA_IPS:
        return True
    addr = ipaddress.ip_address(ip)
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return True
    return addr.version == 4 and addr in _CGNAT_V4


def resolve_and_validate(host: str, port: int) -> list[str]:
    """Resolve ``host`` and validate every IP. Block-on-any (no partial).

    Raises DNSError if resolution fails or yields nothing, SSRFError if any IP is blocked.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DNSError(f"resolution failed for {host!r}") from exc

    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)

    if not ips:
        raise DNSError(f"no addresses for {host!r}")

    for ip in ips:
        if is_blocked_ip(ip):
            raise SSRFError(f"blocked IP for {host!r}: {ip}")

    return ips


async def aresolve_and_validate(
    host: str, port: int, timeout: float | None = None
) -> list[str]:
    """Async form of :func:`resolve_and_validate`: run the blocking resolver off the
    event loop, each attempt bounded by ``timeout`` seconds (default ``DNS_TIMEOUT``).

    Security logic is IDENTICAL - it calls the same sync validator; this only stops a
    slow/hung ``socket.getaddrinfo`` from freezing the single-worker event loop for ALL
    concurrent tool calls (and it re-runs per redirect hop). Only a transient resolver
    answer (EAI_AGAIN) is retried, once: retrying NXDOMAIN is pointless, and retrying a
    timeout would strand a second uncancellable getaddrinfo thread in the shared pool.
    A blocked IP raises SSRFError at once, never retried.
    """
    if timeout is None:
        timeout = DNS_TIMEOUT
    for attempt in range(2):
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.to_thread(resolve_and_validate, host, port)
        except TimeoutError as exc:
            raise DNSError(f"resolution timed out for {host!r} after {timeout:g}s") from exc
        except DNSError as exc:
            cause = exc.__cause__
            transient = isinstance(cause, socket.gaierror) and cause.errno == socket.EAI_AGAIN
            if attempt or not transient:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Opens every TCP connection to a freshly validated IP (anti-DNS-rebinding).

    Pinning at connect time keeps the request URL on the real hostname, so the
    connection pool keys on the hostname, TLS SNI and certificate checks use it,
    and ``response.url`` reports it. The older approach rewrote the URL host to the
    IP: the pool then reused one socket for every hostname behind a shared CDN IP,
    sending host B's request over host A's TLS session (false 403s, B's cert never
    checked) and leaking the IP into ``final_url``.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend,
                 via: tuple[str, int] | None = None) -> None:
        self._inner = inner
        self._via = via  # operator-set CONNECT proxy (ARGUS_EGRESS_PROXY), not user input

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        ips = await aresolve_and_validate(host, port)
        if self._via is None:
            return await self._inner.connect_tcp(
                ips[0], port, timeout=timeout, local_address=local_address,
                socket_options=socket_options,
            )
        try:
            stream = await self._inner.connect_tcp(
                *self._via, timeout=timeout, local_address=local_address,
                socket_options=socket_options,
            )
        except Exception as exc:  # proxy down or unresolvable: callers fall back on this
            raise httpcore.ProxyError(f"egress proxy unreachable: {type(exc).__name__}") from exc
        await connect_tunnel(stream, ips, port, timeout)
        return stream

    async def connect_unix_socket(self, *args, **kwargs):
        raise SSRFError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def parse_proxy(url: str) -> tuple[str, int]:
    """``http://host:port`` -> (host, port). Only plain-HTTP CONNECT proxies without
    credentials are supported. The value is never echoed: it may hold a password."""
    u = httpx.URL(url)
    if u.scheme != "http" or not u.host or u.userinfo or u.port == 0:
        raise ValueError("egress proxy must be http://host:port (no credentials)")
    return u.host, u.port or 80


async def connect_tunnel(stream, ips: list[str], port: int, timeout) -> None:
    """HTTP CONNECT through the egress proxy to an IP we already validated. The proxy is
    handed an address, never a hostname, so it cannot resolve its way somewhere else.
    ``stream`` has httpcore's async stream shape (write/read/aclose)."""
    ip = next((i for i in ips if ":" not in i), ips[0])  # the WARP exit is IPv4 (measured)
    target = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    try:
        await stream.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode(),
                           timeout=timeout)
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = await stream.read(4096, timeout=timeout)
            if not chunk or len(reply) > 8192:
                raise httpcore.ProxyError("egress proxy closed or sent an oversized reply")
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].split(b" ")
        # Bytes past the header block would belong to the TLS stream and be lost here.
        if len(status) < 2 or status[1] != b"200" or not reply.endswith(b"\r\n\r\n"):
            raise httpcore.ProxyError(f"egress proxy refused: {reply[:60]!r}")
    except BaseException as exc:
        await stream.aclose()
        if isinstance(exc, Exception) and not isinstance(exc, httpcore.ProxyError):
            # A hung or reset proxy (ReadTimeout, OSError) is still the proxy failing, and
            # callers fall back to the direct path only on ProxyError.
            raise httpcore.ProxyError(f"egress proxy handshake failed: {exc!r}") from exc
        raise


class _SafeTransport(httpx.AsyncBaseTransport):
    """Validates each request's host before sending; the backend pins the connect.

    The pre-check raises SSRFError before any byte leaves (and keeps transport-level
    mocks such as respx honest); ``_PinnedBackend`` re-validates at connect time,
    which is what actually binds the socket to a public IP.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        port = request.url.port or _DEFAULT_PORTS[request.url.scheme]
        await aresolve_and_validate(request.url.host, port)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _pinned_http_transport(via: tuple[str, int] | None = None) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport()
    # httpx exposes no network_backend knob; the pool attribute is private, so
    # test_ssrf asserts it is ours - an httpx/httpcore upgrade that renames it fails loudly.
    pool = transport._pool
    pool._network_backend = _PinnedBackend(pool._network_backend, via)
    return transport


def build_safe_async_client(via_proxy: str | None = None, **kwargs: object) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that pins connections to validated IPs.

    ``follow_redirects`` defaults to False - the fetch layer re-validates each
    hop itself. SSRFError surfaces at *send* time (when the request is made),
    not at construction. ``via_proxy`` (``http://host:port``) tunnels every pinned
    connection through that CONNECT proxy. Extra **kwargs (timeout, etc.) pass through.
    """
    kwargs.setdefault("follow_redirects", False)
    via = parse_proxy(via_proxy) if via_proxy else None
    transport = _SafeTransport(_pinned_http_transport(via))
    client = httpx.AsyncClient(transport=transport, **kwargs)  # type: ignore[arg-type]
    # These clients are shared by every tool call: one caller's site cookies (anti-bot,
    # paywall, session) must never reach the next. fetch.static keeps a per-call jar.
    client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return client
