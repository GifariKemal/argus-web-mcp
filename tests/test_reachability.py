"""Daily reachability map (argus.reachability)."""

import httpx
import pytest

from argus import config, reachability


def _client(table: dict, calls: list | None = None):
    """MockTransport answering from {host: status | exception class}."""
    def h(req):
        if calls is not None:
            calls.append((req.url.host, req.headers["user-agent"]))
        out = table.get(req.url.host, 200)
        if isinstance(out, type):
            raise out("no answer", request=req)
        if isinstance(out, str):  # a 200 whose body is an anti-bot page
            return httpx.Response(200, text=out)
        return httpx.Response(out, text="<article>" + "real words " * 50 + "</article>")

    return httpx.AsyncClient(transport=httpx.MockTransport(h))


@pytest.fixture
def two_sites(monkeypatch):
    monkeypatch.setattr(reachability, "SITES", {
        "reuters.com": "https://www.reuters.com/world/",
        "example.com": "https://example.com/",
    })
    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")
    monkeypatch.setattr(config, "EGRESS_PROXY_HOSTS", ("reuters.com",))


async def test_measure_records_both_paths_with_argus_user_agent(two_sites):
    calls = []
    direct = _client({"www.reuters.com": 401}, calls)
    warp = _client({}, calls)
    report = await reachability.measure(direct, warp)
    assert report["sites"] == {"reuters.com": {"direct": 401, "warp": 200},
                               "example.com": {"direct": 200, "warp": 200}}
    assert report["at"].endswith("+00:00")
    assert {ua for _, ua in calls} == {reachability._DEFAULT_UA}
    assert reachability.failing_listed(report) == []


async def test_failure_is_retried_once_and_recorded(two_sites):
    calls = []
    warp = _client({"www.reuters.com": httpx.ConnectError}, calls)
    report = await reachability.measure(_client({}), warp)
    assert report["sites"]["reuters.com"]["warp"] == "ConnectError"
    assert calls.count(("www.reuters.com", reachability._DEFAULT_UA)) == 2
    assert reachability.failing_listed(report) == ["reuters.com"]


async def test_unlisted_failures_and_no_proxy_never_alert(two_sites):
    blocked = {"example.com": 403}
    report = await reachability.measure(_client(blocked), _client(blocked))
    assert reachability.failing_listed(report) == []  # example.com is not on the list
    direct_only = await reachability.measure(_client({}))
    assert "warp" not in direct_only["sites"]["reuters.com"]
    assert reachability.failing_listed(direct_only) == []
    assert reachability.failing_listed(None) == []


def test_ok_threshold():
    assert reachability.ok(200) and reachability.ok(302)
    assert not reachability.ok(401) and not reachability.ok(503)
    assert not reachability.ok("ReadTimeout")


async def test_a_200_challenge_page_is_not_reachable(two_sites):
    """Bloomberg-style walls answer 200; the body decides."""
    wall = {"www.reuters.com": "<title>Just a moment...</title>"}
    report = await reachability.measure(_client({}), _client(wall))
    assert report["sites"]["reuters.com"] == {"direct": 200, "warp": "challenge"}
    assert reachability.failing_listed(report) == ["reuters.com"]


async def test_body_read_is_capped(two_sites, monkeypatch):
    """A site streaming a huge body costs at most _MAX_BODY bytes."""
    monkeypatch.setattr(reachability, "_MAX_BODY", 1000)
    sent = []

    async def big(req):
        async def gen():
            for _ in range(100):
                sent.append(1)
                yield b"x" * 500
        return httpx.Response(200, content=gen())

    client = httpx.AsyncClient(transport=httpx.MockTransport(big))
    assert await reachability._probe(client, "https://example.com/") == 200
    assert len(sent) <= 3


def test_confirmed_failing_needs_two_runs(two_sites):
    bad = {"at": "", "sites": {"reuters.com": {"direct": 401, "warp": 403}}}
    good = {"at": "", "sites": {"reuters.com": {"direct": 401, "warp": 200}}}
    assert reachability.confirmed_failing(None, bad) == []
    assert reachability.confirmed_failing(good, bad) == []
    assert reachability.confirmed_failing(bad, bad) == ["reuters.com"]


# --- engine liveness -------------------------------------------------------------------
import respx  # noqa: E402

from argus.reachability import confirmed_dead_engines, measure_engines  # noqa: E402


@respx.mock
async def test_measure_engines_counts_only_attributed_results():
    def answer(request):
        engine = request.url.params["engines"]
        if engine == "dead":  # SearXNG answers from another engine instead
            return httpx.Response(200, json={"results": [{"engine": "bing"}]})
        if engine == "broken":
            return httpx.Response(502)
        hits = [{"engine": engine}, {"engine": engine}, {"engine": "other"}]
        return httpx.Response(200, json={"results": hits})
    respx.get("http://searxng:8080/search").mock(side_effect=answer)
    counts = await measure_engines("http://searxng:8080", ["bing", "dead", "broken"])
    assert counts == {"bing": 2, "dead": 0, "broken": 0}


def test_confirmed_dead_engines_needs_two_runs():
    assert confirmed_dead_engines(None, {"bing": 0}) == []
    assert confirmed_dead_engines({"bing": 3, "brave": 0}, {"bing": 0, "brave": 0}) == ["brave"]


# --- adapter canaries ------------------------------------------------------------------

async def test_measure_adapters_runs_each_canary_through_its_adapter(monkeypatch):
    """An adapter that breaks upstream returns None and the read silently falls back to the
    page (Reddit's feed 404s and X's new web build both went unseen for days in 2026-11)."""
    from argus.fetch import adapters
    from argus.x import fxtwitter

    monkeypatch.setattr(reachability, "CANARIES", {
        "hn": "https://news.ycombinator.com/item?id=1", "npm": "https://www.npmjs.com/package/x",
        "github": "https://github.com/o/r/issues/1", "fxtwitter": "https://x.com/a/status/1"})
    seen = []

    def adapter(result):
        async def f(url, *, client, timeout):
            seen.append(url)
            if isinstance(result, Exception):
                raise result
            return result
        return f

    monkeypatch.setattr(adapters, "fetch_hn", adapter({"html": "ok"}))
    monkeypatch.setattr(adapters, "fetch_npm", adapter(None))
    monkeypatch.setattr(adapters, "fetch_github", adapter(RuntimeError("boom")))
    monkeypatch.setattr(fxtwitter, "fetch_x", adapter({"html": "ok"}))
    out = await reachability.measure_adapters(object())
    assert out == {"hn": True, "npm": False, "github": False, "fxtwitter": True}
    assert len(seen) == 4
    seen.clear()
    assert await reachability.measure_adapters(object(), only=["npm"]) == {"npm": False}
    assert seen == ["https://www.npmjs.com/package/x"]


def test_every_canary_has_an_adapter():
    for name in reachability.CANARIES:
        assert callable(reachability._adapter(name))


def test_confirmed_failing_adapters_needs_two_runs():
    f = reachability.confirmed_failing_adapters
    assert f(None, {"hn": False}) == []
    assert f({"hn": True, "npm": False}, {"hn": False, "npm": False}) == ["npm"]
