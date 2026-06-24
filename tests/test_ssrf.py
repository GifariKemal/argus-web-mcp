"""Tests for the SSRF trust boundary (HARD GATE - 100% line+branch coverage).

Strategy: validate_url is a cheap pre-check (no DNS). is_blocked_ip is a pure
truth table. resolve_and_validate mocks socket.getaddrinfo. The transport in
build_safe_async_client is exercised with a fake inner transport so we can
assert IP-pinning, Host-header preservation, and the sni_hostname extension
(anti-DNS-rebinding) without touching the network.
"""

import socket

import httpx
import pytest

from argus.security.ssrf import (
    ALLOWED_SCHEMES,
    SSRFError,
    build_safe_async_client,
    is_blocked_ip,
    resolve_and_validate,
    validate_url,
)


# --------------------------------------------------------------------------- #
# validate_url
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "ftp://x",
        "file:///etc/passwd",
        "gopher://x",
        "http://",  # no host
        "https:///path",  # no host
    ],
)
def test_validate_url_rejects(url):
    with pytest.raises(SSRFError) as exc:
        validate_url(url)
    assert exc.value.code == "ssrf_blocked"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com:8443/p",
    ],
)
def test_validate_url_allows(url):
    assert validate_url(url) is None


def test_allowed_schemes_contract():
    assert ALLOWED_SCHEMES == {"http", "https"}


# --------------------------------------------------------------------------- #
# is_blocked_ip - truth table
# --------------------------------------------------------------------------- #
BLOCKED_IPS = [
    "127.0.0.1",  # loopback v4
    "10.0.0.1",  # private
    "172.16.0.1",  # private
    "172.31.255.255",  # private edge
    "192.168.1.1",  # private
    "169.254.169.254",  # cloud metadata v4
    "169.254.0.1",  # link-local
    "0.0.0.0",  # unspecified v4
    "::1",  # loopback v6
    "::",  # unspecified v6
    "fd00::1",  # unique-local v6
    "fe80::1",  # link-local v6
    "fc00::1",  # unique-local v6
    "fd00:ec2::254",  # cloud metadata v6
    "224.0.0.1",  # multicast v4
    "100.64.0.1",  # CGNAT low edge
    "100.127.255.255",  # CGNAT high edge
]

ALLOWED_IPS = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "2606:4700:4700::1111",
    "100.63.255.255",  # just below CGNAT
    "100.128.0.1",  # just above CGNAT
]


@pytest.mark.parametrize("ip", BLOCKED_IPS)
def test_is_blocked_ip_blocks(ip):
    assert is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ALLOWED_IPS)
def test_is_blocked_ip_allows(ip):
    assert is_blocked_ip(ip) is False


# --------------------------------------------------------------------------- #
# resolve_and_validate
# --------------------------------------------------------------------------- #
def _gai_result(*ips):
    """Build a getaddrinfo-shaped result for the given IP strings."""
    out = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
        out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return out


def test_resolve_and_validate_mixed_blocks(monkeypatch):
    def fake_gai(host, port, *a, **k):
        return _gai_result("93.184.216.34", "10.0.0.1")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    with pytest.raises(SSRFError):
        resolve_and_validate("evil.example", 443)


def test_resolve_and_validate_all_public(monkeypatch):
    def fake_gai(host, port, *a, **k):
        return _gai_result("93.184.216.34", "2606:4700:4700::1111")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    ips = resolve_and_validate("good.example", 443)
    assert ips == ["93.184.216.34", "2606:4700:4700::1111"]


def test_resolve_and_validate_dedups(monkeypatch):
    """Duplicate addresses from getaddrinfo collapse to one entry."""

    def fake_gai(host, port, *a, **k):
        return _gai_result("93.184.216.34", "93.184.216.34")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    assert resolve_and_validate("dup.example", 443) == ["93.184.216.34"]


def test_resolve_and_validate_gaierror(monkeypatch):
    def fake_gai(host, port, *a, **k):
        raise socket.gaierror("nxdomain")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    with pytest.raises(SSRFError):
        resolve_and_validate("nope.example", 443)


def test_resolve_and_validate_empty(monkeypatch):
    def fake_gai(host, port, *a, **k):
        return []

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    with pytest.raises(SSRFError):
        resolve_and_validate("empty.example", 443)


# --------------------------------------------------------------------------- #
# build_safe_async_client - anti-DNS-rebinding pinning
# --------------------------------------------------------------------------- #
class _RecordingTransport(httpx.AsyncBaseTransport):
    """Fake inner transport: records the rewritten request, returns 200."""

    def __init__(self):
        self.seen: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen = request
        return httpx.Response(200, text="ok")


async def test_client_pins_public_ip(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: _gai_result("93.184.216.34")
    )
    recorder = _RecordingTransport()
    client = build_safe_async_client()
    # Swap the inner transport the safe transport wraps with our recorder.
    client._transport._inner = recorder  # type: ignore[attr-defined]

    resp = await client.get("http://example.com/path?q=1")
    assert resp.status_code == 200

    seen = recorder.seen
    assert seen is not None
    # Connection pinned to the resolved public IP.
    assert seen.url.host == "93.184.216.34"
    # Original Host header preserved.
    assert seen.headers["host"] == "example.com"
    # SNI preserved so TLS still validates against the real hostname.
    assert seen.extensions["sni_hostname"] == "example.com"
    await client.aclose()


async def test_client_pins_ipv6(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: _gai_result("2606:4700:4700::1111")
    )
    recorder = _RecordingTransport()
    client = build_safe_async_client()
    client._transport._inner = recorder  # type: ignore[attr-defined]

    await client.get("https://cloudflare-dns.com/p")
    seen = recorder.seen
    assert seen is not None
    # IPv6 literal is bracketed in the URL host normalization.
    assert seen.url.host == "2606:4700:4700::1111"
    assert seen.headers["host"] == "cloudflare-dns.com"
    assert seen.extensions["sni_hostname"] == "cloudflare-dns.com"
    await client.aclose()


async def test_client_blocks_private_ip(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: _gai_result("10.0.0.1")
    )
    recorder = _RecordingTransport()
    client = build_safe_async_client()
    client._transport._inner = recorder  # type: ignore[attr-defined]

    with pytest.raises(SSRFError):
        await client.get("http://internal.evil/")
    # No request reached the inner transport.
    assert recorder.seen is None
    await client.aclose()


async def test_client_default_port_inference(monkeypatch):
    """URL without explicit port must resolve using the scheme default port."""
    captured = {}

    def fake_gai(host, port, *a, **k):
        captured["host"] = host
        captured["port"] = port
        return _gai_result("8.8.8.8")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    recorder = _RecordingTransport()
    client = build_safe_async_client()
    client._transport._inner = recorder  # type: ignore[attr-defined]

    await client.get("http://dns.google/")  # no port -> default 80
    assert captured["host"] == "dns.google"
    assert captured["port"] == 80
    await client.aclose()


def test_client_does_not_follow_redirects():
    client = build_safe_async_client()
    assert client.follow_redirects is False


def test_client_kwargs_passthrough():
    client = build_safe_async_client(timeout=7.0)
    assert client.timeout.read == 7.0
