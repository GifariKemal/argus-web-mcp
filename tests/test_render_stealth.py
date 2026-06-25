import asyncio
import types

import pytest

from argus.fetch.render import BrowserPool, _looks_blocked


def _result(success=True, html="<html><body>ok content here</body></html>", status=200):
    return types.SimpleNamespace(
        success=success, html=html, status_code=status, url="http://x/", screenshot=None,
        error_message="boom",
    )


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
    monkeypatch.setattr(r, "resolve_and_validate", lambda host, port: ["93.184.216.34"])

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
    monkeypatch.setattr(r, "resolve_and_validate", lambda host, port: ["93.184.216.34"])

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
    monkeypatch.setattr(r, "resolve_and_validate", lambda host, port: ["93.184.216.34"])

    with pytest.raises(FetchError) as ei:
        await pool.render("http://example.com/")
    assert ei.value.code == "blocked_by_antibot"
