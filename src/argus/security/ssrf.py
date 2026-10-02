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

    Raises SSRFError if resolution fails, yields nothing, or any IP is blocked.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError(f"resolution failed for {host!r}") from exc

    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)

    if not ips:
        raise SSRFError(f"no addresses for {host!r}")

    for ip in ips:
        if is_blocked_ip(ip):
            raise SSRFError(f"blocked IP for {host!r}: {ip}")

    return ips


async def aresolve_and_validate(
    host: str, port: int, timeout: float | None = None
) -> list[str]:
    """Async form of :func:`resolve_and_validate`: run the blocking resolver off the
    event loop, bounded by ``timeout`` seconds (default ``DNS_TIMEOUT``).

    Security logic is IDENTICAL - it calls the same sync validator; this only stops a
    slow/hung ``socket.getaddrinfo`` from freezing the single-worker event loop for ALL
    concurrent tool calls (and it re-runs per redirect hop). A timeout surfaces as
    ``SSRFError`` so the existing ``ssrf_blocked`` contract and fetch-layer ladder hold.
    """
    if timeout is None:
        timeout = DNS_TIMEOUT
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.to_thread(resolve_and_validate, host, port)
    except TimeoutError as exc:
        raise SSRFError(f"resolution timed out for {host!r} after {timeout:g}s") from exc


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Opens every TCP connection to a freshly validated IP (anti-DNS-rebinding).

    Pinning at connect time keeps the request URL on the real hostname, so the
    connection pool keys on the hostname, TLS SNI and certificate checks use it,
    and ``response.url`` reports it. The older approach rewrote the URL host to the
    IP: the pool then reused one socket for every hostname behind a shared CDN IP,
    sending host B's request over host A's TLS session (false 403s, B's cert never
    checked) and leaking the IP into ``final_url``.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        ips = await aresolve_and_validate(host, port)
        return await self._inner.connect_tcp(
            ips[0], port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args, **kwargs):
        raise SSRFError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


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


def _pinned_http_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport()
    # httpx exposes no network_backend knob; the pool attribute is private, so
    # test_ssrf asserts it is ours - an httpx/httpcore upgrade that renames it fails loudly.
    pool = transport._pool
    pool._network_backend = _PinnedBackend(pool._network_backend)
    return transport


def build_safe_async_client(**kwargs: object) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that pins connections to validated IPs.

    ``follow_redirects`` defaults to False - the fetch layer re-validates each
    hop itself. SSRFError surfaces at *send* time (when the request is made),
    not at construction. Extra **kwargs (timeout, etc.) pass through.
    """
    kwargs.setdefault("follow_redirects", False)
    transport = _SafeTransport(_pinned_http_transport())
    return httpx.AsyncClient(transport=transport, **kwargs)  # type: ignore[arg-type]
