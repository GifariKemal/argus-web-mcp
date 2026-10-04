"""Chromium egress proxy (security/egress.py): every browser connection is SSRF-gated.

These use real sockets: a stand-in object cannot show whether bytes actually reach an
internal host, which is exactly how the 0.4.20 route-handler attempt passed its tests and
still let a 302 through in the container.
"""

from __future__ import annotations

import asyncio

import pytest

from argus.security import egress
from argus.security.ssrf import SSRFError


@pytest.fixture(autouse=True)
async def _fresh_proxy(monkeypatch):
    # The proxy server is per process; each test runs on its own event loop.
    monkeypatch.setattr(egress, "_server", None)
    yield
    if egress._server is not None:
        egress._server.close()


async def _origin(body: bytes = b"ORIGIN-OK"):
    seen = []

    async def handle(r, w):
        seen.append(await r.readuntil(b"\r\n\r\n"))
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        await w.drain()
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], seen


async def _ask(raw: bytes) -> bytes:
    port = int((await egress.proxy_url()).rsplit(":", 1)[1])
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw)
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    return data


@pytest.mark.parametrize("raw", [
    b"CONNECT 10.0.0.5:443 HTTP/1.1\r\nHost: 10.0.0.5:443\r\n\r\n",
    b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n",
    b"GET http://127.0.0.1:8090/health HTTP/1.1\r\nHost: 127.0.0.1:8090\r\n\r\n",
    b"GET http://[::1]:8090/ HTTP/1.1\r\n\r\n",
    b"GET ftp://example.com/ HTTP/1.1\r\n\r\n",
    b"GET /relative HTTP/1.1\r\n\r\n",
    b"garbage\r\n\r\n",
])
async def test_private_and_malformed_targets_are_refused(raw):
    assert (await _ask(raw)).startswith(b"HTTP/1.1 403")


async def test_real_loopback_service_is_never_reached():
    # The genuine leak scenario: an internal service listening on loopback.
    srv, port, seen = await _origin(b"INTERNAL-SECRET")
    try:
        out = await _ask(f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 403") and b"INTERNAL-SECRET" not in out
        assert seen == []
    finally:
        srv.close()


async def test_public_http_is_forwarded_origin_form_with_connection_close(monkeypatch):
    srv, port, seen = await _origin()

    async def public(host, p, timeout=None):
        if host != "public.test":
            raise SSRFError("blocked")
        return ["127.0.0.1"]  # stand-in for the validated public IP

    monkeypatch.setattr(egress, "aresolve_and_validate", public)
    try:
        out = await _ask(
            f"GET http://public.test:{port}/p?q=1 HTTP/1.1\r\nHost: public.test\r\n"
            "Proxy-Connection: keep-alive\r\n\r\n".encode()
        )
        assert out.endswith(b"ORIGIN-OK")
        req = seen[0]
        assert req.startswith(b"GET /p?q=1 HTTP/1.1\r\n")
        assert b"Proxy-Connection" not in req and b"Connection: close" in req
    finally:
        srv.close()


async def test_connect_tunnel_dials_the_validated_ip(monkeypatch):
    srv, port, seen = await _origin()
    asked = []

    async def public(host, p, timeout=None):
        asked.append((host, p))
        return ["127.0.0.1"]

    monkeypatch.setattr(egress, "aresolve_and_validate", public)
    try:
        proxy_port = int((await egress.proxy_url()).rsplit(":", 1)[1])
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(f"CONNECT pinned.test:{port} HTTP/1.1\r\n\r\n".encode())
        assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        w.write(b"GET / HTTP/1.1\r\nHost: pinned.test\r\n\r\n")
        assert (await asyncio.wait_for(r.read(), 5)).endswith(b"ORIGIN-OK")
        w.close()
        assert asked == [("pinned.test", port)]
    finally:
        srv.close()


async def test_browser_config_routes_everything_through_the_proxy():
    cfg = await egress.guarded_browser_config(headless=True, verbose=False)
    assert cfg.proxy_config.server == await egress.proxy_url()
    assert "--proxy-bypass-list=<-loopback>" in cfg.extra_args


async def test_pipe_survives_a_reset_peer():
    class _Boom:
        async def read(self, n):
            raise ConnectionResetError

    class _Sink:
        closed = False

        def close(self):
            self.closed = True

    sink = _Sink()
    await egress._pipe(_Boom(), sink)  # must not raise
    assert sink.closed


# --------------------------------------------------------------------------- #
# Listed hosts leave through the upstream WARP proxy (ARGUS_EGRESS_PROXY)
# --------------------------------------------------------------------------- #

async def _fake_warp():
    """A CONNECT proxy that records what it was asked and dials it (here: loopback)."""
    asked = []

    async def handle(r, w):
        head = await r.readuntil(b"\r\n\r\n")
        asked.append(head)
        host, _, port = head.split(b" ")[1].decode().rpartition(":")
        up_r, up_w = await asyncio.open_connection(host.strip("[]"), int(port))
        w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await w.drain()
        await asyncio.gather(egress._pipe(r, up_w), egress._pipe(up_r, w))

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], asked


@pytest.fixture
async def _listed(monkeypatch):
    """`listed.test` resolves to the loopback origin and is on the proxy list."""
    from argus import config

    async def public(host, p, timeout=None):
        if host not in ("listed.test", "direct.test"):
            raise SSRFError("blocked")
        return ["127.0.0.1"]

    monkeypatch.setattr(egress, "aresolve_and_validate", public)
    monkeypatch.setattr(config, "EGRESS_PROXY_HOSTS", ("listed.test",))
    warp, warp_port, asked = await _fake_warp()
    monkeypatch.setattr(config, "EGRESS_PROXY", f"http://127.0.0.1:{warp_port}")
    yield asked
    warp.close()


async def test_listed_host_connect_goes_through_warp_to_validated_ip(_listed):
    srv, port, seen = await _origin()
    try:
        proxy_port = int((await egress.proxy_url()).rsplit(":", 1)[1])
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(f"CONNECT listed.test:{port} HTTP/1.1\r\n\r\n".encode())
        assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        w.write(b"GET / HTTP/1.1\r\nHost: listed.test\r\n\r\n")
        assert (await asyncio.wait_for(r.read(), 5)).endswith(b"ORIGIN-OK")
        w.close()
        assert _listed == [f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}"
                           "\r\n\r\n".encode()]
    finally:
        srv.close()


async def test_listed_host_plain_http_goes_through_warp(_listed):
    srv, port, seen = await _origin()
    try:
        out = await _ask(f"GET http://listed.test:{port}/p HTTP/1.1\r\n\r\n".encode())
        assert out.endswith(b"ORIGIN-OK") and seen[0].startswith(b"GET /p HTTP/1.1")
        assert len(_listed) == 1
    finally:
        srv.close()


async def test_unlisted_host_stays_direct(_listed):
    srv, port, _ = await _origin()
    try:
        out = await _ask(f"GET http://direct.test:{port}/ HTTP/1.1\r\n\r\n".encode())
        assert out.endswith(b"ORIGIN-OK") and _listed == []
    finally:
        srv.close()


async def test_listed_private_target_never_reaches_warp(_listed, monkeypatch):
    from argus import config

    monkeypatch.setattr(config, "EGRESS_PROXY_HOSTS", ("listed.test", "10.0.0.5"))
    assert (await _ask(b"CONNECT 10.0.0.5:443 HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 403")
    assert _listed == []


async def test_warp_down_falls_back_to_direct(_listed, monkeypatch):
    from argus import config

    dead = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_port = dead.sockets[0].getsockname()[1]
    dead.close()
    await dead.wait_closed()
    monkeypatch.setattr(config, "EGRESS_PROXY", f"http://127.0.0.1:{dead_port}")
    srv, port, _ = await _origin()
    try:
        out = await _ask(f"GET http://listed.test:{port}/ HTTP/1.1\r\n\r\n".encode())
        assert out.endswith(b"ORIGIN-OK")
    finally:
        srv.close()


async def test_warp_refusal_closes_and_falls_back_to_direct(_listed, monkeypatch):
    from argus import config

    async def refuse(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        await w.drain()

    no = await asyncio.start_server(refuse, "127.0.0.1", 0)
    monkeypatch.setattr(config, "EGRESS_PROXY", f"http://127.0.0.1:{no.sockets[0].getsockname()[1]}")
    srv, port, _ = await _origin()
    try:
        out = await _ask(f"GET http://listed.test:{port}/ HTTP/1.1\r\n\r\n".encode())
        assert out.endswith(b"ORIGIN-OK")
    finally:
        srv.close()
        no.close()


async def test_hung_warp_falls_back_to_direct(_listed, monkeypatch):
    from argus import config

    async def hang(r, w):
        await asyncio.sleep(30)

    stuck = await asyncio.start_server(hang, "127.0.0.1", 0)
    monkeypatch.setattr(config, "EGRESS_PROXY", f"http://127.0.0.1:{stuck.sockets[0].getsockname()[1]}")
    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(egress.asyncio, "wait_for",
                        lambda aw, t: real_wait_for(aw, 0.2 if t == 8 else t))
    srv, port, _ = await _origin()
    try:
        out = await _ask(f"GET http://listed.test:{port}/ HTTP/1.1\r\n\r\n".encode())
        assert out.endswith(b"ORIGIN-OK")
    finally:
        srv.close()
        stuck.close()
