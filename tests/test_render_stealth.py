import asyncio
import types

import pytest

from argus.fetch.render import BrowserPool, _looks_blocked


def _result(success=True, html="<html><body>ok content here</body></html>", status=200):
    return types.SimpleNamespace(
        success=success, html=html, status_code=status, url="http://x/", screenshot=None,
        error_message="boom",
    )


async def _fake_aresolve(host, port, timeout=None):
    """Stub the (now async, off-loop) SSRF resolver: no real DNS, public IP."""
    return ["93.184.216.34"]


def test_looks_blocked_status():
    assert _looks_blocked("<html>fine</html>", 403)
    assert _looks_blocked("<html>fine</html>", 429)
    assert not _looks_blocked("<html>fine</html>", 200)


def test_looks_blocked_markers():
    assert _looks_blocked("<title>Just a moment...</title>", 200)
    assert _looks_blocked("<h1>Attention Required! | Cloudflare</h1>", 200)
    assert not _looks_blocked("<article>real content</article>", 200)


async def test_render_escalates_to_stealth_on_block(monkeypatch):
    pool = BrowserPool()

    class _Crawler:
        def __init__(self, res):
            self.res = res
            self.calls = 0

        async def arun(self, url, config=None):
            self.calls += 1
            return self.res

    normal = _Crawler(_result(success=True, html="<title>Just a moment...</title>", status=503))
    stealth = _Crawler(_result(success=True, html="<article>" + "real " * 50 + "</article>"))
    pool._crawler = normal

    async def fake_ensure():
        pool._stealth = stealth
        return stealth

    monkeypatch.setattr(pool, "_ensure_stealth", fake_ensure)
    # avoid real DNS in resolve_and_validate
    import argus.fetch.render as r
    monkeypatch.setattr(r, "aresolve_and_validate", _fake_aresolve)

    out = await pool.render("http://example.com/")
    assert out["render_tier"] == "stealth"
    assert "real" in out["html"]
    assert normal.calls == 1 and stealth.calls == 1


async def test_render_no_escalation_when_clean(monkeypatch):
    pool = BrowserPool()

    class _Crawler:
        def __init__(self, res):
            self.res = res
            self.calls = 0

        async def arun(self, url, config=None):
            self.calls += 1
            return self.res

    normal = _Crawler(_result(success=True, html="<article>" + "good " * 50 + "</article>"))
    pool._crawler = normal
    escalated = {"n": 0}

    async def fake_ensure():
        escalated["n"] += 1
        return normal

    monkeypatch.setattr(pool, "_ensure_stealth", fake_ensure)
    import argus.fetch.render as r
    monkeypatch.setattr(r, "aresolve_and_validate", _fake_aresolve)

    out = await pool.render("http://example.com/")
    assert out["render_tier"] == "normal"
    assert escalated["n"] == 0


async def test_ensure_stealth_inits_once_under_concurrency(monkeypatch):
    """Two concurrent _ensure_stealth() must start the stealth crawler exactly once (audit R5)."""
    starts = {"n": 0}

    class _StealthCrawler:
        async def start(self):
            starts["n"] += 1
            await asyncio.sleep(0)  # yield: expose the check-then-set race window

    fake_mod = types.SimpleNamespace(
        AsyncWebCrawler=lambda config=None: _StealthCrawler(),
        BrowserConfig=lambda **kw: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "crawl4ai", fake_mod)

    pool = BrowserPool()
    a, b = await asyncio.gather(pool._ensure_stealth(), pool._ensure_stealth())
    assert starts["n"] == 1
    assert a is b is pool._stealth


async def test_render_blocked_raises_antibot_when_stealth_also_fails(monkeypatch):
    from argus.fetch.static import FetchError

    pool = BrowserPool()

    class _Crawler:
        async def arun(self, url, config=None):
            return _result(success=False, html="<title>Just a moment...</title>", status=403)

    pool._crawler = _Crawler()

    async def fake_ensure():
        return _Crawler()

    monkeypatch.setattr(pool, "_ensure_stealth", fake_ensure)
    import argus.fetch.render as r
    monkeypatch.setattr(r, "aresolve_and_validate", _fake_aresolve)

    with pytest.raises(FetchError) as ei:
        await pool.render("http://example.com/")
    assert ei.value.code == "blocked_by_antibot"


class _RecordingCrawler:
    def __init__(self, res):
        self.res = res
        self.calls = 0

    async def arun(self, url, config=None):
        self.calls += 1
        return self.res


def _no_dns(monkeypatch):
    import argus.fetch.render as r

    monkeypatch.setattr(r, "aresolve_and_validate", _fake_aresolve)


async def test_render_blocked_on_both_tiers_raises_antibot(monkeypatch):
    """200 'Just a moment...' on BOTH tiers must raise blocked_by_antibot, never return
    the challenge HTML as content."""
    from argus.fetch.static import FetchError

    pool = BrowserPool()
    challenge = _result(success=True, html="<title>Just a moment...</title>", status=200)
    pool._crawler = _RecordingCrawler(challenge)
    stealth = _RecordingCrawler(challenge)

    async def fake_ensure():
        return stealth

    monkeypatch.setattr(pool, "_ensure_stealth", fake_ensure)
    _no_dns(monkeypatch)

    with pytest.raises(FetchError) as ei:
        await pool.render("http://example.com/")
    assert ei.value.code == "blocked_by_antibot"


async def test_render_blocked_normal_and_failed_stealth_raises_antibot(monkeypatch):
    """Normal tier success-but-blocked + stealth failure: the original challenge page must
    NOT fall through as content."""
    from argus.fetch.static import FetchError

    pool = BrowserPool()
    pool._crawler = _RecordingCrawler(
        _result(success=True, html="<h1>Verify you are human</h1>", status=200)
    )
    stealth = _RecordingCrawler(_result(success=False, html="", status=None))

    async def fake_ensure():
        return stealth

    monkeypatch.setattr(pool, "_ensure_stealth", fake_ensure)
    _no_dns(monkeypatch)

    with pytest.raises(FetchError) as ei:
        await pool.render("http://example.com/")
    assert ei.value.code == "blocked_by_antibot"


async def test_render_direct_stealth_blocked_raises_antibot(monkeypatch):
    """The direct stealth=True path (core.py transport fallback) also refuses a
    success=True challenge page."""
    from argus.fetch.static import FetchError

    pool = BrowserPool()
    pool._crawler = _RecordingCrawler(_result())  # unused
    stealth = _RecordingCrawler(
        _result(success=True, html="<title>Attention Required!</title>", status=200)
    )

    async def fake_ensure():
        return stealth

    monkeypatch.setattr(pool, "_ensure_stealth", fake_ensure)
    _no_dns(monkeypatch)

    with pytest.raises(FetchError) as ei:
        await pool.render("http://example.com/", stealth=True)
    assert ei.value.code == "blocked_by_antibot"


async def test_render_wedged_browser_times_out_and_frees_permit(monkeypatch):
    """A wedged arun (never returns) must raise render_failed within the outer deadline
    and release its semaphore permit - not starve the pool forever."""
    import argus.fetch.render as r
    from argus.fetch.static import FetchError

    monkeypatch.setattr(r, "_RENDER_GRACE_S", 0.01)
    pool = BrowserPool()

    class _Wedged:
        async def arun(self, url, config=None):
            await asyncio.sleep(3600)

    pool._crawler = _Wedged()
    _no_dns(monkeypatch)

    with pytest.raises(FetchError) as ei:
        await pool.render("http://example.com/", timeout=0.01)
    assert ei.value.code == "render_failed"
    assert pool.active_contexts == 0  # permit released

    # pool still usable afterwards
    pool._crawler = _RecordingCrawler(_result(html="<article>" + "fine " * 60 + "</article>"))
    out = await pool.render("http://example.com/", timeout=5)
    assert "fine" in out["html"]


async def test_wedged_stealth_is_recycled_not_reused(monkeypatch):
    """A wedged STEALTH crawler must be closed+dropped so the next call re-inits a fresh
    one - one wedge no longer poisons the anti-bot tier until process restart."""
    import argus.fetch.render as r
    from argus.fetch.static import FetchError

    monkeypatch.setattr(r, "_RENDER_GRACE_S", 0.01)
    starts = {"n": 0}
    closed = {"n": 0}

    class _WedgedStealth:
        async def start(self):
            starts["n"] += 1

        async def close(self):
            closed["n"] += 1

        async def arun(self, url, config=None):
            await asyncio.sleep(3600)  # wedge

    fake_mod = types.SimpleNamespace(
        AsyncWebCrawler=lambda config=None: _WedgedStealth(),
        BrowserConfig=lambda **kw: None,
        CacheMode=types.SimpleNamespace(BYPASS="bypass"),
        CrawlerRunConfig=lambda **kw: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "crawl4ai", fake_mod)
    _no_dns(monkeypatch)

    pool = BrowserPool()
    pool._crawler = _WedgedStealth()  # normal tier present (unused on the direct stealth path)

    with pytest.raises(FetchError) as ei:
        await pool.render("http://example.com/", stealth=True, timeout=0.01)
    assert ei.value.code == "render_failed"
    assert pool._stealth is None  # recycled: wedged handle dropped
    assert starts["n"] == 1 and closed["n"] == 1

    # next stealth render re-inits a fresh crawler instead of reusing the wedged one
    with pytest.raises(FetchError):
        await pool.render("http://example.com/", stealth=True, timeout=0.01)
    assert starts["n"] == 2  # would stay 1 if the wedged handle were reused


async def test_screenshot_of_challenge_page_still_returns(monkeypatch):
    """screenshot=True returns the captured PNG even on a challenge page - 'show me what
    the page looks like' is legitimate, and the bytes are already in hand."""
    pool = BrowserPool()
    challenge = types.SimpleNamespace(
        success=True, html="<title>Just a moment...</title>", status_code=200,
        url="http://x/", screenshot="BASE64PNG", error_message=None,
    )
    pool._crawler = _RecordingCrawler(challenge)
    stealth = _RecordingCrawler(challenge)

    async def fake_ensure():
        return stealth

    monkeypatch.setattr(pool, "_ensure_stealth", fake_ensure)
    _no_dns(monkeypatch)

    out = await pool.render("http://example.com/", screenshot=True)
    assert out["screenshot"] == "BASE64PNG"
