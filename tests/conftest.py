"""Shared fixtures.

Integration tests use an httpx.MockTransport "fixture server" instead of a real socket
server: the SSRF guard would block loopback (127.0.0.1) by design, so a real local server
can't be reached through the guarded client. MockTransport + a public-IP getaddrinfo stub
gives the same end-to-end coverage (tool -> fetch -> extract -> cache) fully offline.
"""

import ipaddress
import socket

import fitz  # pymupdf
import httpx
import pytest

ARTICLE_HTML = (
    "<html><head><title>Gold Outlook</title></head><body>"
    "<nav>home about</nav>"
    "<article><h1>Gold Outlook</h1>"
    "<p>" + ("Gold prices are driven by real yields and the dollar. " * 8) + "</p>"
    "<p>" + ("Central bank demand has supported the metal this year. " * 6) + "</p>"
    "</article><footer>copyright</footer></body></html>"
)

THIN_HTML = "<html><head><script>app()</script></head><body><div id='root'></div></body></html>"
RICH_RENDERED = (
    "<html><body><article>" + ("Rendered content sentence here. " * 30) + "</article></body></html>"
)

STRUCT_HTML = (
    "<html><body><h1>Widget</h1>"
    "<span class='price' data-v='9.99'>$9.99</span>"
    "<ul><li>a</li><li>b</li></ul></body></html>"
)


def _make_pdf() -> bytes:
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Argus PDF page one alpha")
    p2 = doc.new_page()
    p2.insert_text((72, 72), "Argus PDF page two beta")
    data = doc.tobytes()
    doc.close()
    return data


PDF_BYTES = _make_pdf()


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/article":
        return httpx.Response(200, text=ARTICLE_HTML)
    if path == "/thin":
        return httpx.Response(200, text=THIN_HTML)
    if path == "/struct":
        return httpx.Response(200, text=STRUCT_HTML)
    if path == "/doc.pdf":
        return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
    if path == "/old":
        return httpx.Response(301, headers={"location": "/article"})
    return httpx.Response(404, text="not found")


@pytest.fixture
def public_dns(monkeypatch):
    """Make every hostname resolve to a public IP so the SSRF guard allows fixture hosts."""

    def _gai(host, port, *a, **k):
        # IP literals resolve to themselves (so SSRF private/metadata tests still block);
        # hostnames map to a public IP so guarded fixture hosts are allowed.
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = "93.184.216.34"
        if ":" in ip:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _gai)


class FakeBrowser:
    """Stands in for BrowserPool in offline integration tests."""

    def __init__(self, html: str = RICH_RENDERED):
        self.html = html
        self.calls = 0

    async def render(self, url, *, wait_for=None, actions=None, screenshot=False,
                     full_page=True, timeout=45, stealth=False):
        self.calls += 1
        shot = "BASE64PNG" if screenshot else None
        return {"final_url": url, "html": self.html, "screenshot": shot, "render_tier": "normal"}


@pytest.fixture
def app_state(tmp_path, public_dns, monkeypatch):
    """Build + install server state with a MockTransport client, tmp cache, fake browser.

    LLM is neutralised by default so no test ever hits a real API even if OPENAI_API_KEY is
    set in the environment; tests that exercise the LLM path re-enable it explicitly.
    """
    from argus import server
    from argus.cache import Cache
    from argus.watch import WatchStore

    monkeypatch.setattr(server, "llm_available", lambda: False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    cache = Cache(db_path=str(tmp_path / "cache.db"), blob_dir=str(tmp_path / "blobs"))
    state = server.State(client=client, cache=cache, browser=FakeBrowser(),
                         watch_store=WatchStore(str(tmp_path / "watches.json")))
    server._S = state
    yield state
    server._S = None


BASE = "http://fixtures.test"
