import socket

import httpx
import pytest

from argus.fetch.core import _visible_text_len, fetch
from argus.fetch.fallback import fetch_via_archive
from argus.fetch.render import BrowserPool
from argus.fetch.static import FetchError, fetch_static
from argus.security.ssrf import SSRFError

ARTICLE = "<html><body><article>" + ("word " * 80) + "</article></body></html>"
THIN = "<html><head><script>var x=1</script></head><body><div id=root></div></body></html>"


def _gai(mapping):
    def _f(host, port, *a, **k):
        ip = mapping.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return _f


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _FakeBrowser:
    def __init__(self, html="<html><body>" + ("rich " * 80) + "</body></html>"):
        self.html = html
        self.calls = 0

    async def render(self, url, *, wait_for=None, actions=None, screenshot=False,
                     full_page=True, timeout=45, stealth=False):
        self.calls += 1
        return {"final_url": url, "html": self.html, "screenshot": "b64" if screenshot else None}


async def test_static_happy(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        return httpx.Response(200, text=ARTICLE)

    async with _client(h) as c:
        res = await fetch("http://example.com/", client=c)
    assert res["render_path"] == "static"
    assert res["status"] == 200
    assert "word" in res["html"]


async def test_ssrf_metadata_ip_blocked_no_request():
    # resolve_and_validate raises before any client.get - client=None proves no call.
    with pytest.raises(SSRFError):
        await fetch("http://169.254.169.254/", client=None)


async def test_ssrf_localhost_blocked():
    with pytest.raises(SSRFError):
        await fetch("http://localhost/", client=None)


async def test_redirect_to_private_blocked_on_hop(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _gai({"example.com": "93.184.216.34", "evil.internal": "10.0.0.1"}),
    )

    def h(req):
        if req.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://evil.internal/"})
        return httpx.Response(200, text=ARTICLE)

    async with _client(h) as c:
        with pytest.raises(SSRFError):
            await fetch("http://example.com/", client=c)


async def test_redirect_followed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text=ARTICLE)

    async with _client(h) as c:
        res = await fetch_static("http://example.com/old", client=c)
    assert res["final_url"].endswith("/new")
    assert res["status"] == 200


async def test_too_many_redirects(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        return httpx.Response(302, headers={"location": "/loop"})

    async with _client(h) as c:
        with pytest.raises(FetchError) as ei:
            await fetch_static("http://example.com/loop", client=c, max_redirects=2)
    assert ei.value.code == "fetch_failed"


async def test_timeout_becomes_fetch_error(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        raise httpx.TimeoutException("slow")

    async with _client(h) as c:
        with pytest.raises(FetchError) as ei:
            await fetch("http://example.com/", client=c)
    assert ei.value.code == "fetch_failed"


async def test_oversized_response_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        return httpx.Response(200, text="x", headers={"content-length": str(64 * 1024 * 1024)})

    async with _client(h) as c:
        with pytest.raises(FetchError) as ei:
            await fetch_static("http://example.com/big", client=c)
    assert ei.value.code == "fetch_failed"


async def test_streaming_cap_rejects_chunked_no_length(monkeypatch):
    # No Content-Length header => the header fast-path can't catch it. The
    # streaming hard-cap must abort once the accumulated body exceeds the cap.
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    monkeypatch.setattr("argus.fetch.static.MAX_FETCH_BYTES", 1000)

    def h(req):
        # Async generator body => transfer-encoding: chunked, NO content-length
        # header, so only the streaming hard-cap (not the header fast-path) stops it.
        async def body():
            for _ in range(5):
                yield b"x" * 1000  # 5000 bytes total

        return httpx.Response(200, content=body())

    async with _client(h) as c:
        with pytest.raises(FetchError) as ei:
            await fetch_static("http://example.com/stream", client=c)
    assert ei.value.code == "fetch_failed"
    assert "too large" in str(ei.value)


async def test_streaming_under_cap_returns_normally(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    monkeypatch.setattr("argus.fetch.static.MAX_FETCH_BYTES", 1000)

    def h(req):
        async def body():
            yield b"hello world" * 5  # 55 bytes, chunked (no content-length)

        return httpx.Response(200, content=body())

    async with _client(h) as c:
        res = await fetch_static("http://example.com/ok", client=c)
    assert res["status"] == 200
    assert res["html"].startswith("hello world")
    assert res["render_path"] == "static"


async def test_streaming_cap_applies_to_fetch_bytes(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    monkeypatch.setattr("argus.fetch.static.MAX_FETCH_BYTES", 1000)

    def h(req):
        async def body():
            for _ in range(4):
                yield b"y" * 1000  # 4000 bytes, chunked (no content-length)

        return httpx.Response(200, content=body())

    from argus.fetch.static import fetch_bytes

    async with _client(h) as c:
        with pytest.raises(FetchError) as ei:
            await fetch_bytes("http://example.com/bigbytes", client=c)
    assert ei.value.code == "fetch_failed"


async def test_fetch_bytes_happy_returns_content(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        return httpx.Response(
            200, content=b"%PDF-1.7 body", headers={"content-type": "application/pdf"}
        )

    from argus.fetch.static import fetch_bytes

    async with _client(h) as c:
        final_url, content, ctype = await fetch_bytes("http://example.com/doc.pdf", client=c)
    assert content == b"%PDF-1.7 body"
    assert ctype == "application/pdf"
    assert final_url.endswith("/doc.pdf")


async def test_thin_static_escalates_to_browser(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        return httpx.Response(200, text=THIN)

    fake = _FakeBrowser()
    async with _client(h) as c:
        res = await fetch("http://example.com/", client=c, browser=fake)
    assert res["render_path"] == "browser"
    assert fake.calls == 1
    assert "rich" in res["html"]


async def test_browser_escalation_failure_falls_back_to_static(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    class _Broken(_FakeBrowser):
        async def render(self, *a, **k):
            raise FetchError("render_failed", "boom")

    def h(req):
        return httpx.Response(200, text=THIN)

    async with _client(h) as c:
        res = await fetch("http://example.com/", client=c, browser=_Broken())
    assert res["render_path"] == "static"


async def test_explicit_render_without_browser_errors():
    with pytest.raises(FetchError) as ei:
        await fetch("http://example.com/", render=True, browser=None)
    assert ei.value.code == "render_failed"


async def test_explicit_render_uses_browser(monkeypatch):
    fake = _FakeBrowser()
    res = await fetch("http://example.com/", render=True, screenshot=True, browser=fake)
    assert res["render_path"] == "browser"
    assert res["screenshot"] == "b64"


def test_visible_text_len_ignores_scripts():
    assert _visible_text_len(THIN) < 10
    assert _visible_text_len(ARTICLE) >= 80


# --- egress fallback: static transport failure recovery ---------------------

SNAPSHOT_URL = "http://web.archive.org/web/20240101000000/http://blocked.example/"
SNAPSHOT_HTML = "<html><body><article>" + ("snap " * 80) + "</article></body></html>"


def _avail_json(snapshot_url):
    return (
        '{"archived_snapshots":{"closest":{"available":true,"url":"'
        + snapshot_url
        + '","status":"200"}}}'
    )


async def test_static_connecterror_falls_back_to_stealth_browser(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        raise httpx.ConnectError("blocked egress")

    class _Stealth(_FakeBrowser):
        def __init__(self):
            super().__init__()
            self.stealth_seen = None

        async def render(self, url, *, stealth=False, timeout=45, **k):
            self.stealth_seen = stealth
            self.calls += 1
            return {"final_url": url, "html": self.html, "screenshot": None}

    fake = _Stealth()
    async with _client(h) as c:
        res = await fetch("http://blocked.example/", client=c, browser=fake)
    assert res["render_path"] == "browser"
    assert fake.calls == 1
    assert fake.stealth_seen is True
    assert "rich" in res["html"]


async def test_static_connecterror_no_browser_falls_back_to_archive(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        host = req.url.host
        if host == "blocked.example":
            raise httpx.ConnectError("blocked egress")
        if host == "archive.org":
            return httpx.Response(200, text=_avail_json(SNAPSHOT_URL))
        # the snapshot host
        return httpx.Response(200, text=SNAPSHOT_HTML)

    async with _client(h) as c:
        res = await fetch("http://blocked.example/", client=c, browser=None)
    assert res["render_path"] == "archive"
    assert "snap" in res["html"]
    assert res["status"] == 200


async def test_static_connecterror_no_browser_no_snapshot_reraises(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        host = req.url.host
        if host == "blocked.example":
            raise httpx.ConnectError("blocked egress")
        # availability API answers, but there is no snapshot
        return httpx.Response(200, text='{"archived_snapshots":{}}')

    async with _client(h) as c:
        with pytest.raises(FetchError) as ei:
            await fetch("http://blocked.example/", client=c, browser=None)
    assert ei.value.code == "fetch_failed"


async def test_ssrf_blocked_url_never_falls_back():
    # A blocked host raises SSRFError from the guard *before* any HTTP call.
    # The fallback chain only catches FetchError, so SSRFError must propagate
    # and neither the browser nor the archive may be touched.
    class _NeverBrowser:
        async def render(self, *a, **k):
            raise AssertionError("browser must not be called on SSRF block")

    with pytest.raises(SSRFError):
        await fetch("http://169.254.169.254/", client=None, browser=_NeverBrowser())


async def test_fetch_via_archive_returns_dict_on_success(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        if req.url.host == "archive.org":
            return httpx.Response(200, text=_avail_json(SNAPSHOT_URL))
        return httpx.Response(200, text=SNAPSHOT_HTML)

    async with _client(h) as c:
        res = await fetch_via_archive("http://blocked.example/", client=c)
    assert res is not None
    assert res["render_path"] == "archive"
    assert "snap" in res["html"]


async def test_fetch_via_archive_returns_none_when_no_snapshot(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        return httpx.Response(200, text='{"archived_snapshots":{}}')

    async with _client(h) as c:
        res = await fetch_via_archive("http://blocked.example/", client=c)
    assert res is None


async def test_fetch_via_archive_returns_none_on_archive_error(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        raise httpx.ConnectError("archive unreachable")

    async with _client(h) as c:
        res = await fetch_via_archive("http://blocked.example/", client=c)
    assert res is None


@pytest.mark.browser
async def test_browserpool_real_render():
    pool = BrowserPool()
    await pool.start()
    try:
        r = await pool.render("https://example.com/")
        assert "Example Domain" in r["html"]
    finally:
        await pool.stop()
