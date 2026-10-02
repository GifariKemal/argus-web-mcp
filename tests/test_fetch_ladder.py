"""Fetch-ladder fixes from the 2026-10-02 production logs: DNS failures get their own code,
repeat failures fail fast, a rate-limited Wayback is skipped, 5xx pages are not content,
and block detection uses crawl4ai's vendor fingerprints."""

import socket

import httpx
import pytest

import argus.fetch.core as core
import argus.fetch.fallback as fallback
import argus.models as models
from argus.fetch.core import fetch
from argus.fetch.fallback import fetch_via_archive
from argus.fetch.render import _looks_blocked
from argus.fetch.static import FetchError, fetch_static
from argus.security.ssrf import DNSError, SSRFError

ARTICLE = "<html><body><article>" + ("word " * 80) + "</article></body></html>"
SNAPSHOT = "http://web.archive.org/web/20240101000000/http://blocked.example/"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(core, "_negative", {})
    monkeypatch.setattr(fallback, "_off_until", 0.0)
    monkeypatch.setattr(fallback, "_transport_fails", 0)
    models.STAGE_COUNTS.clear()
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                      ("93.184.216.34", port))],
    )


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _avail(url):
    return '{"archived_snapshots":{"closest":{"available":true,"url":"' + url + '"}}}'


# --- DNS --------------------------------------------------------------------------- #
async def test_dns_failure_is_dns_failed_and_skips_fallbacks(monkeypatch):
    def nxdomain(*a, **k):
        raise socket.gaierror("Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", nxdomain)

    class _NeverBrowser:
        async def render(self, *a, **k):
            raise AssertionError("a DNS failure must not enter the ladder")

    with pytest.raises(DNSError) as ei:
        await fetch("http://nope.example/", client=None, browser=_NeverBrowser())
    assert ei.value.code == "dns_failed"
    assert isinstance(ei.value, SSRFError)
    assert "fetch.static_fail" not in models.STAGE_COUNTS


# --- static 5xx -------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [500, 502, 504, 520, 522, 526])
async def test_static_5xx_is_a_failure(status):
    async with _client(lambda r: httpx.Response(status, text=ARTICLE)) as c:
        with pytest.raises(FetchError) as ei:
            await fetch_static("http://example.com/", client=c)
    assert ei.value.code == "fetch_failed"
    assert ei.value.status == status


async def test_static_5xx_goes_down_the_ladder():
    def h(req):
        if req.url.host == "blocked.example":
            return httpx.Response(522, text=ARTICLE)
        if req.url.host == "archive.org":
            return httpx.Response(200, text=_avail(SNAPSHOT))
        return httpx.Response(200, text=ARTICLE.replace("word", "snap"))

    async with _client(h) as c:
        res = await fetch("http://blocked.example/", client=c)
    assert res["render_path"] == "archive"
    assert "snap" in res["html"]


# --- negative cache ---------------------------------------------------------------- #
def _exhausting_handler(calls):
    def h(req):
        calls.append(req.url.host)
        if req.url.host == "blocked.example":
            return httpx.Response(403, text="denied")
        return httpx.Response(200, text='{"archived_snapshots":{}}')

    return h


async def test_exhausted_url_fails_fast_on_repeat():
    calls = []
    async with _client(_exhausting_handler(calls)) as c:
        with pytest.raises(FetchError) as first:
            await fetch("http://blocked.example/", client=c)
        n = len(calls)
        with pytest.raises(FetchError) as again:
            await fetch("http://blocked.example/", client=c)
    assert len(calls) == n  # no request at all on the repeat
    assert first.value.code == again.value.code == "blocked_by_antibot"
    assert models.STAGE_COUNTS["fetch.negative_cache_hit"] == 1


async def test_negative_entry_expires(monkeypatch):
    calls = []
    async with _client(_exhausting_handler(calls)) as c:
        with pytest.raises(FetchError):
            await fetch("http://blocked.example/", client=c)
        n = len(calls)
        monkeypatch.setattr(core.time, "monotonic", lambda: 1e12)
        with pytest.raises(FetchError):
            await fetch("http://blocked.example/", client=c)
    assert len(calls) > n  # expired -> the ladder ran again
    assert "fetch.negative_cache_hit" not in models.STAGE_COUNTS


async def test_browser_requests_always_get_a_fresh_attempt():
    # scrape is the escalation an agent tries after a failed read; a cached wall from an
    # earlier attempt must not refuse it, and a forced render never poisons a later read.
    class _Blocked:
        calls = 0

        async def render(self, *a, **k):
            self.calls += 1
            raise FetchError("blocked_by_antibot", "challenge persists")

    b = _Blocked()
    for _ in range(2):
        with pytest.raises(FetchError):
            await fetch("http://example.com/", render=True, browser=b)
    assert b.calls == 2
    from argus.fetch import core

    assert "http://example.com/" not in core._negative


async def test_forced_render_other_failure_is_not_remembered():
    class _Broken:
        calls = 0

        async def render(self, *a, **k):
            self.calls += 1
            raise FetchError("render_failed", "crash")

    b = _Broken()
    for _ in range(2):
        with pytest.raises(FetchError):
            await fetch("http://example.com/", render=True, browser=b)
    assert b.calls == 2


def test_negative_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(core, "_NEGATIVE_MAX", 3)
    for i in range(5):
        core._remember_failure(f"http://h{i}/", FetchError("fetch_failed", "x"))
    assert list(core._negative) == ["http://h2/", "http://h3/", "http://h4/"]


# --- Wayback ----------------------------------------------------------------------- #
async def test_archive_429_records_stage_and_cools_down():
    calls = []

    def h(req):
        calls.append(req.url.host)
        return httpx.Response(429, text="slow down")

    async with _client(h) as c:
        assert await fetch_via_archive("http://a.example/", client=c) is None
        assert await fetch_via_archive("http://b.example/", client=c) is None
    assert calls == ["archive.org"]  # the second call never left the box
    assert models.STAGE_COUNTS["fetch.archive_fail_429"] == 1
    assert models.STAGE_COUNTS["fetch.archive_skip_cooldown"] == 1


async def test_archive_cooldown_ends(monkeypatch):
    async with _client(lambda r: httpx.Response(429)) as c:
        await fetch_via_archive("http://a.example/", client=c)
    monkeypatch.setattr(fallback.time, "monotonic", lambda: 1e12)
    async with _client(lambda r: httpx.Response(200, text='{"archived_snapshots":{}}')) as c:
        assert await fetch_via_archive("http://a.example/", client=c) is None
    assert models.STAGE_COUNTS["fetch.archive_fail_none"] == 1


async def test_archive_transport_failures_cool_down_after_three():
    calls = []

    def h(req):
        calls.append(1)
        raise httpx.ConnectError("unreachable")

    async with _client(h) as c:
        for _ in range(4):
            await fetch_via_archive("http://a.example/", client=c)
    assert len(calls) == 3
    assert models.STAGE_COUNTS["fetch.archive_fail_transport"] == 3
    assert models.STAGE_COUNTS["fetch.archive_skip_cooldown"] == 1


async def test_archive_answer_resets_transport_count():
    state = {"down": True}

    def h(req):
        if state["down"]:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, text='{"archived_snapshots":{}}')

    async with _client(h) as c:
        for down in (True, True, False, True, True):
            state["down"] = down
            await fetch_via_archive("http://a.example/", client=c)
    assert fallback._off_until == 0.0  # never three failures in a row


async def test_archive_bad_json_records_other():
    async with _client(lambda r: httpx.Response(200, text="not json")) as c:
        assert await fetch_via_archive("http://a.example/", client=c) is None
    assert models.STAGE_COUNTS["fetch.archive_fail_other"] == 1


async def test_archive_fetches_raw_snapshot_with_capped_timeout():
    seen = []

    def h(req):
        seen.append((str(req.url), req.extensions["timeout"]["read"]))
        if req.url.host == "archive.org":
            return httpx.Response(200, text=_avail(SNAPSHOT))
        return httpx.Response(200, text=ARTICLE)

    async with _client(h) as c:
        res = await fetch_via_archive("http://blocked.example/", client=c, timeout=30)
    assert res["render_path"] == "archive"
    assert seen[1][0] == "http://web.archive.org/web/20240101000000id_/http://blocked.example/"
    assert {t for _, t in seen} == {10}


# --- block detection ---------------------------------------------------------------- #
def test_docs_page_titled_access_denied_is_not_blocked():
    """An AWS docs page about the S3 "Access Denied" error is content, not a wall."""
    body = "".join(f"<p>Step {i}: check the bucket policy and IAM permissions.</p>"
                   for i in range(300))
    html = ("<html><head><title>Access Denied - Amazon S3 troubleshooting</title></head>"
            f"<body><main><h1>Access Denied</h1>{body}</main></body></html>")
    assert len(html) > 10_000
    assert not _looks_blocked(html, 200)


def test_datadome_captcha_page_is_blocked():
    html = ("<html><head><title>example.com</title></head><body>"
            "<script>var dd={'rt':'c','cid':'AHrlqAAAAAMA','host':'geo.captcha-delivery.com'}"
            "</script><script src=\"https://ct.captcha-delivery.com/c.js\"></script>"
            "</body></html>")
    assert _looks_blocked(html, 200)


def test_tiny_legit_page_is_not_blocked():
    """crawl4ai's structural/near-empty verdicts mean thin, not blocked; a crawl4ai
    upgrade that renames those reasons fails here."""
    assert not _looks_blocked("<html><body><p>ok</p></body></html>", 200)
    assert not _looks_blocked("<p>no body tag</p>", 200)


def test_article_quoting_a_bot_wall_is_content_not_a_block():
    from argus.fetch.render import _looks_blocked

    body = "<p>" + "How bot walls work and what users see. " * 200 + "</p>"
    quoted = ("<html><body><h1>Inside Imperva</h1><p>The page says "
              "'Pardon Our Interruption' and shows Reference #18.abc.</p>"
              + body + "</body></html>")
    assert _looks_blocked(quoted, 200) is False
    wall = ("<html><body><h1>Pardon Our Interruption</h1><p>As you were browsing something "
            "about your browser made us think you were a bot.</p></body></html>")
    assert _looks_blocked(wall, 200) is True
