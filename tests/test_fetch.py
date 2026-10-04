import asyncio
import socket

import httpx
import pytest

from argus import config
from argus.fetch.core import (
    ESCALATE_BELOW_CHARS,
    STATIC_FALLBACK_TIMEOUT,
    _visible_text_len,
    fetch,
)
from argus.fetch.fallback import fetch_via_archive
from argus.fetch.render import BrowserPool
from argus.fetch.static import FetchError, fetch_static
from argus.security.ssrf import SSRFError, build_safe_async_client

ARTICLE = "<html><body><article>" + ("word " * 80) + "</article></body></html>"
THIN = "<html><head><script>var x=1</script></head><body><div id=root></div></body></html>"


@pytest.fixture(autouse=True)
def _fresh_ladder_state(monkeypatch):
    """The negative cache and the archive cool-down are process-wide; isolate each test."""
    import argus.fetch.core as core
    import argus.fetch.fallback as fallback

    monkeypatch.setattr(core, "_negative", {})
    monkeypatch.setattr(fallback, "_off_until", 0.0)
    monkeypatch.setattr(fallback, "_transport_fails", 0)


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
                     timeout=45, stealth=False):
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


async def test_just_above_threshold_does_not_escalate(monkeypatch):
    # visible text >= ESCALATE_BELOW_CHARS -> keep the static result, never call browser.
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    body = "<html><body>" + ("a " * ESCALATE_BELOW_CHARS) + "</body></html>"
    assert _visible_text_len(body) >= ESCALATE_BELOW_CHARS

    def h(req):
        return httpx.Response(200, text=body)

    fake = _FakeBrowser()
    async with _client(h) as c:
        res = await fetch("http://example.com/", client=c, browser=fake)
    assert res["render_path"] == "static"
    assert fake.calls == 0


async def test_just_below_threshold_escalates(monkeypatch):
    # visible text < ESCALATE_BELOW_CHARS -> escalate to the browser tier.
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    body = "<html><body>" + ("a" * (ESCALATE_BELOW_CHARS - 1)) + "</body></html>"
    assert _visible_text_len(body) < ESCALATE_BELOW_CHARS

    def h(req):
        return httpx.Response(200, text=body)

    fake = _FakeBrowser()
    async with _client(h) as c:
        res = await fetch("http://example.com/", client=c, browser=fake)
    assert res["render_path"] == "browser"
    assert fake.calls == 1


async def test_thin_static_kept_when_escalation_render_raises(monkeypatch):
    # The escalation render itself raises FetchError -> the thin STATIC result is
    # returned (not an error); content/status come from the static hop.
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    class _Broken(_FakeBrowser):
        async def render(self, *a, **k):
            raise FetchError("render_failed", "render crashed")

    def h(req):
        return httpx.Response(200, text=THIN)

    async with _client(h) as c:
        res = await fetch("http://example.com/", client=c, browser=_Broken())
    assert res["render_path"] == "static"
    assert res["status"] == 200
    assert "root" in res["html"]  # the thin static body, not a browser render


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


async def test_static_timeout_is_capped_when_browser_fallback_exists(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    seen = {}

    def h(req):
        seen.update(req.extensions["timeout"])
        raise httpx.TimeoutException("slow origin")

    fake = _FakeBrowser()
    async with _client(h) as c:
        res = await fetch("http://blocked.example/", client=c, browser=fake, timeout=60)

    assert seen["read"] == STATIC_FALLBACK_TIMEOUT
    assert res["render_path"] == "browser"
    assert fake.calls == 1


async def test_static_timeout_not_capped_without_browser(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    seen = {}

    def h(req):
        seen.setdefault(req.url.host, req.extensions["timeout"]["read"])
        raise httpx.TimeoutException("slow origin")

    async with _client(h) as c:
        with pytest.raises(FetchError):
            await fetch("http://blocked.example/", client=c, browser=None, timeout=60)

    assert seen["blocked.example"] == 60
    assert seen["archive.org"] == 10  # the archive step is capped on its own


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


async def test_fetch_via_archive_percent_encodes_target_url(monkeypatch):
    """The target URL is percent-encoded into the availability query so its own `&`
    cannot inject extra query params into the archive.org request (Sec hardening)."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    seen = {}

    def h(req):
        if req.url.host == "archive.org":
            seen["raw"] = str(req.url)
            return httpx.Response(200, text='{"archived_snapshots":{}}')
        return httpx.Response(200, text=SNAPSHOT_HTML)

    target = "http://blocked.example/?a=1&b=2"
    async with _client(h) as c:
        await fetch_via_archive(target, client=c)
    # The raw `&` from the target must NOT appear unencoded; it is %26-encoded.
    assert "%26b%3D2" in seen["raw"]
    assert "blocked.example/?a=1&b=2" not in seen["raw"]


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_status_block_escalates_to_stealth_browser(monkeypatch, status):
    """An anti-bot status block (403/429/503) from the static tier must escalate to the
    stealth browser instead of returning the challenge page as content. Regression guard
    for the fix: fetch.core only escalated on `except FetchError`, which a non-2xx never
    raised, so the stealth+archive ladder never fired on status blocks."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        return httpx.Response(status, text="<html><body>Access denied (cf-challenge)</body></html>")

    browser = _FakeBrowser()
    async with _client(h) as c:
        res = await fetch("http://example.com/blocked", client=c, browser=browser)
    assert browser.calls == 1  # escalation fired
    assert res["render_path"] == "browser"
    assert "rich" in res["html"]  # real browser content, not the challenge page
    assert "Access denied" not in res["html"]


# --- encoding: sniff meta-declared charset when the HTTP header carries none ---
async def test_static_sniffs_meta_charset_windows1251(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    text = "Привет мир"
    body = ('<html><head><meta charset="windows-1251"></head><body>'
            + text + "</body></html>").encode("windows-1251")

    def h(req):
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    async with _client(h) as c:
        res = await fetch_static("http://example.com/", client=c)
    assert "�" not in res["html"]  # no mojibake
    assert text in res["html"]


async def test_static_sniffs_shift_jis_http_equiv(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    text = "こんにちは"
    body = ('<html><head><meta http-equiv="Content-Type" '
            'content="text/html; charset=shift_jis"></head><body>'
            + text + "</body></html>").encode("shift_jis")

    def h(req):
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    async with _client(h) as c:
        res = await fetch_static("http://example.com/", client=c)
    assert "�" not in res["html"]
    assert text in res["html"]


async def test_static_honors_header_charset_over_meta(monkeypatch):
    """A header-declared charset wins; the meta-sniff must not override it."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    text = "café"
    body = ("<html><body>" + text + "</body></html>").encode("latin-1")

    def h(req):
        return httpx.Response(
            200, content=body, headers={"content-type": "text/html; charset=latin-1"}
        )

    async with _client(h) as c:
        res = await fetch_static("http://example.com/", client=c)
    assert text in res["html"]


async def test_static_bogus_meta_charset_falls_back_to_utf8(monkeypatch):
    """An unknown/bogus meta-declared charset must fall back to utf-8, not raise LookupError."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    body = (b'<html><head><meta charset="bogus-enc-xyz"></head><body>'
            b"hello world</body></html>")

    def h(req):
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    async with _client(h) as c:
        res = await fetch_static("http://example.com/", client=c)
    assert res["status"] == 200
    assert "hello world" in res["html"]  # decoded via utf-8 fallback, no exception


# --------------------------------------------------------------------------- #
# Pipeline-stage observability (Wave 1): fetch-ladder hops record STAGE_COUNTS.
# --------------------------------------------------------------------------- #
async def test_static_ok_records_stage(monkeypatch):
    import argus.models as models
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    models.STAGE_COUNTS.clear()

    def h(req):
        return httpx.Response(200, text=ARTICLE)

    async with _client(h) as c:
        await fetch("http://example.com/", client=c)
    assert models.STAGE_COUNTS.get("fetch.static_ok") == 1


async def test_archive_fallback_records_stages(monkeypatch):
    import argus.models as models
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    models.STAGE_COUNTS.clear()

    def h(req):
        host = req.url.host
        if host == "blocked.example":
            raise httpx.ConnectError("blocked egress")
        if host == "archive.org":
            return httpx.Response(200, text=_avail_json(SNAPSHOT_URL))
        return httpx.Response(200, text=SNAPSHOT_HTML)

    async with _client(h) as c:
        await fetch("http://blocked.example/", client=c, browser=None)
    assert models.STAGE_COUNTS.get("fetch.static_fail") == 1
    assert models.STAGE_COUNTS.get("fetch.fallback_archive_ok") == 1


async def test_exhausted_fallback_records_stage(monkeypatch):
    import argus.models as models
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    models.STAGE_COUNTS.clear()

    def h(req):
        host = req.url.host
        if host == "blocked.example":
            raise httpx.ConnectError("blocked egress")
        return httpx.Response(200, text='{"archived_snapshots":{}}')

    async with _client(h) as c:
        with pytest.raises(FetchError):
            await fetch("http://blocked.example/", client=c, browser=None)
    assert models.STAGE_COUNTS.get("fetch.fallback_exhausted") == 1


async def _origin_server(body=b"VIA-PROXY"):
    async def handle(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        await w.drain()
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def _connect_proxy(mode="tunnel"):
    """A real CONNECT proxy. tunnel: dial loopback whatever IP it was asked for (the IP is
    recorded, so the test sees what Argus sent); refuse: 403; hang: never answer."""
    asked = []

    async def handle(r, w):
        head = await r.readuntil(b"\r\n\r\n")
        asked.append(head.split(b"\r\n", 1)[0].decode())
        if mode == "hang":
            await asyncio.sleep(30)
            return
        if mode == "refuse":
            w.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await w.drain()
            w.close()
            return
        port = int(head.split(b" ")[1].rsplit(b":", 1)[1])
        up_r, up_w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await w.drain()

        async def pipe(a, b):
            while data := await a.read(65536):
                b.write(data)
                await b.drain()
            b.close()

        await asyncio.gather(pipe(r, up_w), pipe(up_r, w))

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], asked


@pytest.fixture
def _egress(monkeypatch):
    """listed.test is proxied; every name resolves to a public IP (the real resolver path)."""
    import argus.fetch.static as static

    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    monkeypatch.setattr(config, "EGRESS_PROXY_HOSTS", ("listed.test",))
    monkeypatch.setattr(static, "_proxied", {})
    stages = []
    monkeypatch.setattr(static, "record_stage", stages.append)
    return stages


_DIRECT = _client(lambda req: httpx.Response(200, text="DIRECT"))


async def test_egress_end_to_end_through_a_real_connect_proxy(_egress, monkeypatch):
    """Real sockets, real build_safe_async_client(via_proxy=...): the proxy is asked for
    the validated IP, never the hostname, and the body comes back through the tunnel."""
    origin, oport = await _origin_server()
    proxy, pport, asked = await _connect_proxy()
    monkeypatch.setattr(config, "EGRESS_PROXY", f"http://127.0.0.1:{pport}")
    try:
        res = await fetch_static(f"http://listed.test:{oport}/a", client=_DIRECT, timeout=5)
        assert res["html"] == "VIA-PROXY"
        assert asked == [f"CONNECT 93.184.216.34:{oport} HTTP/1.1"]
        assert _egress == ["fetch.egress_proxy"]
    finally:
        origin.close()
        proxy.close()


@pytest.mark.parametrize("mode", ["refuse", "hang", "dead"])
async def test_egress_failing_proxy_falls_back_to_direct(_egress, monkeypatch, mode):
    import argus.fetch.static as static

    monkeypatch.setattr(static, "PROXY_CONNECT_TIMEOUT", 0.3)
    proxy, pport, _ = await _connect_proxy(mode if mode != "dead" else "refuse")
    if mode == "dead":
        proxy.close()
        await proxy.wait_closed()
    monkeypatch.setattr(config, "EGRESS_PROXY", f"http://127.0.0.1:{pport}")
    try:
        res = await fetch_static("http://listed.test/a", client=_DIRECT, timeout=5)
        assert res["html"] == "DIRECT"
        assert _egress == ["fetch.egress_proxy", "fetch.egress_proxy_fail"]
    finally:
        proxy.close()


async def test_egress_routes_per_hop(_egress, monkeypatch):
    """Each redirect hop picks its own path: unlisted -> listed -> unlisted."""
    import argus.fetch.static as static

    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")
    seen = []

    def direct(req):
        seen.append(("direct", req.url.host))
        if req.url.host == "start.test":
            return httpx.Response(302, headers={"location": "https://listed.test/x"})
        return httpx.Response(200, text=ARTICLE)

    def proxied(req):
        seen.append(("proxy", req.url.host))
        return httpx.Response(302, headers={"location": "https://end.test/"})

    monkeypatch.setattr(static, "_proxied_client", lambda via: _client(proxied))
    res = await fetch_static("https://start.test/", client=_client(direct))
    assert res["status"] == 200
    assert seen == [("direct", "start.test"), ("proxy", "listed.test"), ("direct", "end.test")]


async def test_cookie_wall_completes_but_never_leaks_between_calls(_egress, monkeypatch):
    """set-cookie + 302 back to itself (DataDome, consent) needs the cookie on the next
    hop; the next CALL must start clean, because the clients are shared."""
    import argus.fetch.static as static

    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")
    cookies = []

    def wall(req):
        cookies.append(req.headers.get("cookie"))
        if req.headers.get("cookie") != "pass=1":
            return httpx.Response(302, headers={"location": str(req.url),
                                                "set-cookie": "pass=1; Path=/"})
        return httpx.Response(200, text=ARTICLE)

    shared = build_safe_async_client()  # the real policy: the client jar keeps nothing
    shared._transport = httpx.MockTransport(wall)
    monkeypatch.setattr(static, "_proxied_client", lambda via: shared)
    for _ in range(2):
        assert (await fetch_static("https://listed.test/", client=_DIRECT))["status"] == 200
    assert cookies == [None, "pass=1", None, "pass=1"]
    assert not shared.cookies


async def test_egress_proxy_off_by_default(monkeypatch):
    import argus.fetch.static as static

    monkeypatch.setattr(static, "_proxied", {})
    monkeypatch.setattr(config, "EGRESS_PROXY", "")
    assert static._egress_client_for("archive.org") is None
    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")
    monkeypatch.setattr(config, "EGRESS_PROXY_HOSTS", ("archive.org",))
    assert static._egress_client_for("notarchive.org") is None
    assert static._egress_client_for("archive.org.evil.com") is None
    c = static._egress_client_for("WEB.ARCHIVE.org.")
    assert c is not None and static._egress_client_for("archive.org") is c  # cached per loop


async def test_fetch_via_archive_falls_back_to_cdx_when_availability_is_empty(monkeypatch):
    """/wayback/available often answers 'no snapshot' for pages that have one; the CDX
    API is asked next, and its latest 200 capture is read in raw id_ form."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    seen = []

    def h(req):
        seen.append(str(req.url))
        if req.url.host == "archive.org":
            return httpx.Response(200, text='{"archived_snapshots":{}}')
        if req.url.path == "/cdx/search/cdx":
            return httpx.Response(200, text='[["timestamp","original"],'
                                            '["20261003215945","https://www.python.org/"]]')
        return httpx.Response(200, text=SNAPSHOT_HTML)

    async with _client(h) as c:
        res = await fetch_via_archive("https://python.org/?a=1&b=2", client=c)
    assert res is not None and res["render_path"] == "archive"
    assert "url=https%3A%2F%2Fpython.org%2F%3Fa%3D1%26b%3D2" in seen[1]
    assert seen[2] == "https://web.archive.org/web/20261003215945id_/https://www.python.org/"


@pytest.mark.parametrize("cdx", [
    lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)),
    lambda req: httpx.Response(200, text='[["timestamp","original"]]'),  # no capture
    lambda req: httpx.Response(200, text="<html>not json</html>"),
])
async def test_fetch_via_archive_cdx_miss_is_none_without_cooldown(monkeypatch, cdx):
    """CDX times out often (3 of 7 measured); that must not count toward the archive
    cool-down, which would switch the whole step off for 30 minutes."""
    import argus.fetch.fallback as fallback

    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        if req.url.host == "archive.org":
            return httpx.Response(200, text='{"archived_snapshots":{}}')
        return cdx(req)

    async with _client(h) as c:
        for _ in range(4):
            assert await fetch_via_archive("http://blocked.example/", client=c) is None
    assert fallback._off_until == 0.0 and fallback._transport_fails == 0


async def test_egress_tls_failure_through_tunnel_falls_back_to_direct(_egress, monkeypatch):
    """CONNECT worked but TLS through it failed (ConnectError): go direct, it re-guards."""
    import argus.fetch.static as static

    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")

    def tls_fails(req):
        raise httpx.ConnectError("tls handshake failed", request=req)

    monkeypatch.setattr(static, "_proxied_client", lambda via: _client(tls_fails))
    res = await fetch_static("https://listed.test/", client=_DIRECT)
    assert res["html"] == "DIRECT"
    assert _egress == ["fetch.egress_proxy", "fetch.egress_proxy_fail"]


async def test_cookie_never_crosses_to_another_host(_egress, monkeypatch):
    """A cookie set by host A must not ride a 302 to host B."""
    cookies = {}

    def h(req):
        cookies[req.url.host] = req.headers.get("cookie")
        if req.url.host == "a.test":
            return httpx.Response(302, headers={"location": "https://b.test/",
                                                "set-cookie": "sid=secret; Path=/"})
        return httpx.Response(200, text=ARTICLE)

    shared = build_safe_async_client()
    shared._transport = httpx.MockTransport(h)
    assert (await fetch_static("https://a.test/", client=shared))["status"] == 200
    assert cookies == {"a.test": None, "b.test": None}


async def test_fetch_via_archive_whole_step_has_a_deadline(monkeypatch):
    import argus.fetch.fallback as fallback

    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    monkeypatch.setattr(fallback, "ARCHIVE_DEADLINE", 0.2)
    stages = []
    monkeypatch.setattr(fallback, "record_stage", stages.append)

    async def slow(req):
        await asyncio.sleep(5)
        return httpx.Response(200, text="{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as c:
        assert await fetch_via_archive("http://blocked.example/", client=c) is None
    assert stages == ["fetch.archive_fail_deadline"]
    assert fallback._off_until == 0.0 and fallback._transport_fails == 0


async def test_datadome_401_is_an_antibot_block(monkeypatch):
    """DataDome answers 401 with x-datadome (Reuters, WSJ, measured). That is a wall, not
    content: it goes down the ladder. Other 401s (API auth, Basic auth) stay content."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        if req.url.host == "wall.test":
            return httpx.Response(401, headers={"x-datadome": "protected"},
                                  text="Please enable JS and disable any ad blocker")
        if req.url.host == "basic.test":
            return httpx.Response(401, headers={"www-authenticate": 'Basic realm="x"'},
                                  text="auth required")
        return httpx.Response(401, json={"message": "Requires authentication"})

    async with _client(h) as c:
        with pytest.raises(FetchError) as ei:
            await fetch_static("https://wall.test/", client=c)
        assert (ei.value.code, ei.value.status) == ("blocked_by_antibot", 401)
        assert (await fetch_static("https://basic.test/", client=c))["status"] == 401
        assert (await fetch_static("https://api.test/user", client=c))["status"] == 401


class _InternalLanding:
    """A browser whose page always redirects to an internal host (egress proxy refused)."""

    calls = 0

    async def render(self, url, **kw):
        self.calls += 1
        raise SSRFError("blocked IP for 'searxng': 172.20.0.2")


async def test_read_ladder_internal_landing_still_tries_wayback(monkeypatch):
    """In a plain read the URL itself passed the gate; a stealth render that redirected
    internally is one failed rung, and the Wayback copy of the URL is still fair."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        if req.url.host == "blocked.example":
            return httpx.Response(403, text="denied")
        if req.url.host == "archive.org":
            return httpx.Response(200, text=_avail_json(SNAPSHOT_URL))
        return httpx.Response(200, text=SNAPSHOT_HTML)

    browser = _InternalLanding()
    async with _client(h) as c:
        res = await fetch("http://blocked.example/", client=c, browser=browser)
    assert res["render_path"] == "archive" and browser.calls == 1


async def test_read_thin_page_redirecting_internally_keeps_static_result(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    async with _client(lambda req: httpx.Response(200, text=THIN)) as c:
        res = await fetch("http://thin.example/", client=c, browser=_InternalLanding())
    assert res["render_path"] == "static"


async def test_forced_render_internal_landing_raises_ssrf(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    with pytest.raises(SSRFError):
        await fetch("http://example.com/", client=None, browser=_InternalLanding(), render=True)
