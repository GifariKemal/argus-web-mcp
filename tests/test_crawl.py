"""Tests for the deep-crawl module.

Offline unit tests cover the three seams that don't need a real browser:
SSRF gating on the seed, config/filter-chain construction (_build_config), and
result shaping (_shape). A single @pytest.mark.browser test exercises a real
tiny crawl against crawl4ai.com.
"""

import socket
from types import SimpleNamespace

import pytest

from argus.fetch.crawl import _build_config, _shape, deep_crawl
from argus.security.ssrf import SSRFError


def _gai(ip: str):
    """getaddrinfo stub that returns a single fixed IPv4 result."""

    def inner(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return inner


# --- SSRF gating on the seed -------------------------------------------------


async def test_bad_scheme_raises_ssrf():
    with pytest.raises(SSRFError):
        await deep_crawl("ftp://example.com/")


async def test_metadata_ip_seed_raises_ssrf(monkeypatch):
    # The seed host resolves to the cloud metadata IP -> resolve_and_validate blocks.
    monkeypatch.setattr(socket, "getaddrinfo", _gai("169.254.169.254"))
    with pytest.raises(SSRFError):
        await deep_crawl("http://169.254.169.254/")


async def test_private_host_seed_raises_ssrf(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai("10.0.0.5"))
    with pytest.raises(SSRFError):
        await deep_crawl("http://internal.test/")


# --- config / filter-chain construction --------------------------------------


def _filters(chain):
    return list(getattr(chain, "filters", []))


def _filter_types(chain):
    return [type(f).__name__ for f in _filters(chain)]


def test_build_config_flags():
    cfg = _build_config(
        "https://example.com/start",
        depth=3,
        max_pages=20,
        include=None,
        exclude=None,
        same_domain=True,
        respect_robots=True,
    )
    assert cfg.check_robots_txt is True
    assert cfg.stream is False
    strat = cfg.deep_crawl_strategy
    assert strat.max_depth == 3
    assert strat.max_pages == 20


def test_build_config_robots_off():
    cfg = _build_config(
        "https://example.com/",
        depth=1,
        max_pages=5,
        include=None,
        exclude=None,
        same_domain=True,
        respect_robots=False,
    )
    assert cfg.check_robots_txt is False


def test_build_config_domain_filter_present_for_same_domain():
    from crawl4ai import DomainFilter

    cfg = _build_config(
        "https://example.com/path",
        depth=1,
        max_pages=5,
        include=None,
        exclude=None,
        same_domain=True,
        respect_robots=True,
    )
    chain = cfg.deep_crawl_strategy.filter_chain
    domain_filters = [f for f in _filters(chain) if isinstance(f, DomainFilter)]
    assert len(domain_filters) == 1
    assert "example.com" in domain_filters[0]._allowed_domains


def test_build_config_no_domain_filter_when_cross_domain():
    from crawl4ai import DomainFilter

    cfg = _build_config(
        "https://example.com/",
        depth=1,
        max_pages=5,
        include=None,
        exclude=None,
        same_domain=False,
        respect_robots=True,
    )
    chain = cfg.deep_crawl_strategy.filter_chain
    assert not any(isinstance(f, DomainFilter) for f in _filters(chain))


def test_build_config_include_exclude_become_url_pattern_filters():
    from crawl4ai import URLPatternFilter

    cfg = _build_config(
        "https://example.com/",
        depth=1,
        max_pages=5,
        include=["*/docs/*"],
        exclude=["*/private/*"],
        same_domain=False,
        respect_robots=True,
    )
    chain = cfg.deep_crawl_strategy.filter_chain
    pattern_filters = [f for f in _filters(chain) if isinstance(f, URLPatternFilter)]
    # one include filter + one exclude (reverse=True) filter
    assert len(pattern_filters) == 2
    assert any(getattr(f, "reverse", False) for f in pattern_filters)
    assert any(not getattr(f, "reverse", False) for f in pattern_filters)


# --- result shaping ----------------------------------------------------------


def _fake_result(url, *, title=None, markdown="", success=True, depth=0, links=None):
    md = SimpleNamespace(raw_markdown=markdown) if markdown else SimpleNamespace(raw_markdown="")
    return SimpleNamespace(
        url=url,
        success=success,
        markdown=md,
        metadata={"title": title, "depth": depth} if title else {"depth": depth},
        links=links or {},
        error_message=None if success else "boom",
    )


def test_shape_basic():
    results = [
        _fake_result(
            "https://example.com/",
            title="Home",
            markdown="# Home\nbody",
            depth=0,
            links={"internal": [{"href": "https://example.com/a"}]},
        ),
        _fake_result("https://example.com/a", title="A", markdown="A body", depth=1),
    ]
    out = _shape(results)
    assert out["count"] == 2
    urls = [p["url"] for p in out["pages"]]
    assert urls == ["https://example.com/", "https://example.com/a"]
    home = out["pages"][0]
    assert home["title"] == "Home"
    assert home["depth"] == 0
    assert "Home" in home["content"]
    assert out["link_graph"]["https://example.com/"] == ["https://example.com/a"]


def test_shape_drops_failed_results():
    results = [
        _fake_result("https://example.com/", markdown="ok", success=True),
        _fake_result("https://example.com/bad", markdown="", success=False, depth=1),
    ]
    out = _shape(results)
    assert out["count"] == 1
    assert out["pages"][0]["url"] == "https://example.com/"
    assert "https://example.com/bad" not in out["link_graph"]


def test_shape_empty():
    out = _shape([])
    assert out == {"pages": [], "link_graph": {}, "count": 0}


def test_build_config_page_timeout_wired():
    cfg = _build_config(
        "https://example.com/", depth=1, max_pages=5, include=None, exclude=None,
        same_domain=True, respect_robots=True, timeout=45,
    )
    assert cfg.page_timeout == 45000


async def test_deep_crawl_holds_pool_semaphore(monkeypatch):
    """A pool-backed crawl must hold one BrowserPool permit while it runs."""
    import asyncio
    import socket as _socket

    monkeypatch.setattr(_socket, "getaddrinfo", _gai("93.184.216.34"))

    sem = asyncio.Semaphore(1)
    seen = {}

    class _Crawler:
        async def arun(self, url, config=None):
            seen["locked_during_run"] = sem.locked()
            return []

    browser = SimpleNamespace(_crawler=_Crawler(), _sem=sem)
    out = await deep_crawl("https://example.com/", browser=browser)
    assert seen["locked_during_run"] is True  # permit held during the crawl
    assert sem._value == 1  # released afterwards
    assert out["count"] == 0


# --- live browser ------------------------------------------------------------


@pytest.mark.browser
async def test_deep_crawl_live():
    out = await deep_crawl("https://crawl4ai.com/", depth=1, max_pages=3)
    assert out["count"] >= 1
    assert any(p["content"] for p in out["pages"])


# --- server-tool wall clock + clamps (offline) --------------------------------


async def test_server_crawl_times_out_with_err_dict(monkeypatch):
    """A never-returning deep_crawl must yield a structured err dict within the tool
    timeout - before the fix this hung forever (TIMEOUTS['crawl'] was dead config)."""
    import asyncio

    import argus.server as server

    async def wedged(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "deep_crawl", wedged)
    monkeypatch.setattr(server, "_S", SimpleNamespace(browser=object(), client=None,
                                                      cache=None, throttle=None,
                                                      watch_store=None))
    out = await server.crawl("https://example.com/", timeout=0.05)
    assert out["code"] == "fetch_failed"
    assert "timed out" in out["error"]


async def test_server_crawl_catches_unexpected_exceptions(monkeypatch):
    """Raw crawler exceptions must come back as the structured error contract."""
    import argus.server as server

    async def boom(*a, **k):
        raise RuntimeError("browser exploded")

    monkeypatch.setattr(server, "deep_crawl", boom)
    monkeypatch.setattr(server, "_S", SimpleNamespace(browser=object(), client=None,
                                                      cache=None, throttle=None,
                                                      watch_store=None))
    out = await server.crawl("https://example.com/")
    assert out["code"] == "fetch_failed"
    assert out["detail"] == "RuntimeError"  # sanitized, never the message


async def test_server_crawl_clamps_depth_and_max_pages(monkeypatch):
    import argus.server as server

    got = {}

    async def capture(seed, *, depth, max_pages, **k):
        got["depth"], got["max_pages"] = depth, max_pages
        return {"pages": [], "link_graph": {}, "count": 0}

    monkeypatch.setattr(server, "deep_crawl", capture)
    monkeypatch.setattr(server, "_S", SimpleNamespace(browser=object(), client=None,
                                                      cache=None, throttle=None,
                                                      watch_store=None))
    await server.crawl("https://example.com/", depth=99, max_pages=10_000)
    assert got == {"depth": 5, "max_pages": 200}
