"""Loopback egress proxy that every Chromium connection must go through.

A Playwright route handler only sees the first request of a redirect chain, so a public
page that 302s to ``http://searxng:8080`` slipped past one (live-confirmed in 0.4.20
testing). Chromium is instead launched with ``--proxy-server`` pointing here: each new
connection - every redirect hop, subresource, ``fetch()`` and WebSocket - arrives as a
CONNECT or an absolute-URI request, is resolved and validated by the same SSRF gate as the
httpx tier, and is dialled to the validated IP. Chromium never resolves DNS itself, so this
also pins against rebinding.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit

from ..models import record_stage
from .ssrf import SSRFError, aresolve_and_validate

_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HOP_HEADERS = (b"proxy-connection:", b"connection:", b"keep-alive:", b"proxy-authorization:")

_server: asyncio.base_events.Server | None = None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


def _target(method: str, target: str) -> tuple[str, int]:
    """(host, port) a proxy request asks for. CONNECT carries host:port; anything else
    must be an absolute http:// URI (https always arrives as CONNECT)."""
    if method == "CONNECT":
        host, _, port = target.rpartition(":")
        return host.strip("[]"), int(port)
    u = urlsplit(target)
    if u.scheme != "http" or not u.hostname:
        raise SSRFError(f"unsupported proxy target {target[:80]!r}")
    return u.hostname, u.port or 80


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), 30)
        line, _, rest = head.partition(b"\r\n")
        method, target, version = line.decode("latin-1").split(" ", 2)
        host, port = _target(method, target)
        ips = await aresolve_and_validate(host, port)
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(ips[0], port), 15)
    except SSRFError:
        record_stage("fetch.browser_ssrf_blocked")
        client_w.write(_FORBIDDEN)
        client_w.close()
        return
    except Exception:  # noqa: BLE001 - malformed request / unreachable host: refuse, never raise
        client_w.write(_FORBIDDEN)
        client_w.close()
        return
    if method == "CONNECT":
        client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    else:
        # One request per connection: forcing Connection: close stops Chromium from
        # reusing this tunnel for a different host after the first response.
        u = urlsplit(target)
        path = urlunsplit(("", "", u.path or "/", u.query, ""))
        headers = [h for h in rest.split(b"\r\n") if h and not h.lower().startswith(_HOP_HEADERS)]
        up_w.write(f"{method} {path} {version}\r\n".encode("latin-1")
                   + b"\r\n".join([*headers, b"Connection: close"]) + b"\r\n\r\n")
    await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))


async def proxy_url() -> str:
    """Start the proxy once per process (lazily) and return its http://127.0.0.1:port URL."""
    global _server
    # No lock: the first call is BrowserPool.start() in the lifespan, before any tool runs.
    # Restart if the server was closed or belongs to an event loop that is gone.
    stale = _server is None or not _server.is_serving() or (
        _server.get_loop() is not asyncio.get_running_loop()
    )
    if stale:
        _server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    return f"http://127.0.0.1:{_server.sockets[0].getsockname()[1]}"


async def guarded_browser_config(**kwargs):
    """A crawl4ai BrowserConfig whose Chromium sends every connection through the proxy.
    ``<-loopback>`` removes Chromium's implicit localhost bypass, so 127.0.0.1 and
    ``localhost`` targets hit the gate too instead of going direct."""
    from crawl4ai import BrowserConfig

    return BrowserConfig(
        proxy_config={"server": await proxy_url()},
        extra_args=["--proxy-bypass-list=<-loopback>"],
        **kwargs,
    )
