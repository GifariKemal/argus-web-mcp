"""A dead shared Chromium is relaunched instead of failing every render until redeploy."""

from __future__ import annotations

import asyncio
import types

from argus.fetch import render
from argus.fetch.render import BrowserPool, _browser_dead

_DEAD = "BrowserType.new_context: Target page, context or browser has been closed"


def _res(success=True, error="", html="<html>" + "x" * 6000 + "</html>"):
    return types.SimpleNamespace(success=success, error_message=error, html=html,
                                 status_code=200, url="https://e.com/", redirected_url=None)


def test_dead_markers():
    assert _browser_dead(_res(False, _DEAD))
    assert not _browser_dead(_res(False, "net::ERR_NAME_NOT_RESOLVED"))
    assert not _browser_dead(_res(True, _DEAD))


class _Crawler:
    def __init__(self, results):
        self.results, self.closed = list(results), False
        self.crawler_strategy = types.SimpleNamespace(browser_manager=types.SimpleNamespace(
            browser=types.SimpleNamespace(is_connected=lambda: not self.closed)))

    async def arun(self, url, config=None):
        return self.results.pop(0)

    async def close(self):
        self.closed = True


async def test_dead_browser_is_relaunched_and_the_render_retried(monkeypatch):
    pool = BrowserPool()
    dead = _Crawler([_res(False, _DEAD)])
    dead.closed = True  # process gone: is_connected() -> False
    fresh = _Crawler([_res()])
    pool._crawler = dead

    async def start():
        pool._crawler = fresh

    monkeypatch.setattr(pool, "start", start)
    monkeypatch.setattr(render, "aresolve_and_validate", lambda *a, **k: asyncio.sleep(0))
    out = await pool.render("https://e.com/")
    assert dead.closed and pool._crawler is fresh
    assert out["html"].startswith("<html>")


async def test_concurrent_renders_relaunch_once(monkeypatch):
    pool = BrowserPool()
    dead = _Crawler([_res(False, _DEAD), _res(False, _DEAD)])
    starts = []

    async def start():
        starts.append(1)
        await asyncio.sleep(0)
        pool._crawler = _Crawler([_res()])

    pool._crawler = dead
    monkeypatch.setattr(pool, "start", start)
    await pool._restart_normal(dead)
    await pool._restart_normal(dead)  # second caller sees the new crawler, does nothing
    assert starts == [1]


def test_alive_reflects_the_browser_process():
    pool = BrowserPool()
    assert pool.alive() is False  # never started
    pool._crawler = c = _Crawler([])
    assert pool.alive() is True
    c.closed = True
    assert pool.alive() is False
    pool._crawler = object()  # crawl4ai internals moved: do not flip health by itself
    assert pool.alive() is True


async def test_page_level_closed_error_does_not_relaunch(monkeypatch):
    # "has been closed" also means one tab crashed or called window.close(); while the
    # browser process is still connected, the shared Chromium must be left alone.
    pool = BrowserPool()
    live = _Crawler([_res(False, _DEAD)])
    pool._crawler = live
    pool._stealth = _Crawler([_res(False, "net::ERR_ABORTED")])  # the escalation also fails
    monkeypatch.setattr(pool, "start", lambda: (_ for _ in ()).throw(AssertionError("relaunch")))
    monkeypatch.setattr(render, "aresolve_and_validate", lambda *a, **k: asyncio.sleep(0))
    try:
        await pool.render("https://e.com/")
    except render.FetchError:
        pass
    assert pool._crawler is live and not live.closed
