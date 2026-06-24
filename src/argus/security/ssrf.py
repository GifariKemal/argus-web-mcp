"""SSRF trust boundary for Argus.

Follows the OWASP SSRF cheat sheet: scheme allowlist, resolve-then-validate,
deny private/metadata/reserved ranges, and anti-DNS-rebinding by pinning the
outgoing connection to a *validated* resolved IP while preserving the original
Host header and TLS SNI.

This is a HARD GATE. Keep it small, pure, and fully covered.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

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


class _SafeTransport(httpx.AsyncBaseTransport):
    """Pins each request to a validated resolved IP (anti-DNS-rebinding).

    On every request we resolve+validate the host, rewrite the URL host to the
    first validated IP, restore the original Host header, and set the
    ``sni_hostname`` extension so TLS validates against the real hostname.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        port = request.url.port or _DEFAULT_PORTS[request.url.scheme]

        ips = resolve_and_validate(original_host, port)
        pinned = ips[0]

        request.url = request.url.copy_with(host=pinned)
        request.headers["host"] = original_host
        request.extensions["sni_hostname"] = original_host

        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_safe_async_client(**kwargs: object) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that pins connections to validated IPs.

    ``follow_redirects`` defaults to False - the fetch layer re-validates each
    hop itself. SSRFError surfaces at *send* time (when the request is made),
    not at construction. Extra **kwargs (timeout, etc.) pass through.
    """
    kwargs.setdefault("follow_redirects", False)
    transport = _SafeTransport(httpx.AsyncHTTPTransport())
    return httpx.AsyncClient(transport=transport, **kwargs)  # type: ignore[arg-type]
