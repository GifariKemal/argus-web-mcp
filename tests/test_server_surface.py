"""The MCP-facing surface: what clients see in tools/list and in call results (0.4.20)."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus import __version__, server


async def _tools() -> dict:
    return {t.name: t for t in await server.mcp.list_tools()}


async def test_every_tool_carries_annotations():
    tools = await _tools()
    assert len(tools) == 20
    for name, t in tools.items():
        ann = t.annotations
        assert ann is not None, name
        if name in ("watch", "unwatch"):
            assert ann.read_only_hint is False
        else:
            assert ann.read_only_hint is True, name
    assert tools["unwatch"].annotations.destructive_hint is True


async def test_content_tools_raise_the_client_result_cap():
    tools = await _tools()
    for name in ("read", "scrape", "batch_read", "read_pdf", "research", "crawl"):
        assert (tools[name].meta or {}).get("anthropic/maxResultSizeChars") == 500_000, name
    assert "anthropic/maxResultSizeChars" not in (tools["search"].meta or {})


def test_single_version_source():
    pyproject = tomllib.loads(Path(__file__).parents[1].joinpath("pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__
    assert server.mcp.version == __version__


async def test_error_results_are_flagged_is_error(app_state):
    # Through the real MCP call path, so the middleware runs: an err() dict must reach the
    # client as a failed call, a normal dict must not.
    bad = await server.mcp.call_tool("read_pdf", {})
    assert bad.is_error is True
    assert bad.structured_content["code"] == "schema_invalid"
    good = await server.mcp.call_tool("list_watches", {})
    assert good.is_error is False


async def test_health_public_body_is_status_only(app_state):
    app_state.browser._crawler = object()  # FakeBrowser has no Chromium handle
    public = await server.health(SimpleNamespace(client=SimpleNamespace(host="10.11.0.9")))
    body = bytes(public.body).decode()
    assert '"status"' in body and "tool_latencies" not in body and "uptime" not in body
    local = await server.health(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))
    assert "tool_latencies" in bytes(local.body).decode()


async def test_watch_cap_and_webhook_masking(app_state, monkeypatch):
    monkeypatch.setattr(server, "_MAX_WATCHES", 2)
    hook = "https://api.telegram.org/bot123:SECRET/sendMessage?chat_id=1"
    for i in range(2):
        assert "id" in await server.watch(f"https://example.com/{i}", hook)
    capped = await server.watch("https://example.com/3", hook)
    assert capped["code"] == "schema_invalid"
    listed = await server.list_watches()
    assert {w["webhook"] for w in listed["watches"]} == {"https://api.telegram.org"}


async def test_research_max_sources_is_clamped(app_state, monkeypatch):
    seen = {}

    async def fake_research(query, **kw):
        seen.update(kw)
        return {"query": query, "sources": [], "count": 0}

    monkeypatch.setattr(server, "_research", fake_research)
    await server.research("q", max_sources=1000)
    assert seen["max_sources"] == 10


async def test_metrics_is_loopback_only(app_state):
    app_state.browser.active_contexts = 0
    app_state.browser._crawler = object()
    public = await server.metrics(SimpleNamespace(client=SimpleNamespace(host="10.11.0.9")))
    assert public.status_code == 403
    local = await server.metrics(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))
    assert b"argus_up 1" in bytes(local.body)


async def test_health_without_peer_fails_closed(app_state):
    app_state.browser._crawler = object()
    blind = await server.health(SimpleNamespace(client=None))
    assert "tool_latencies" not in bytes(blind.body).decode()


def test_webhook_mask_drops_credentials():
    assert server._mask_url("https://user:pw@hooks.example.com:8443/x?t=1") == (
        "https://hooks.example.com:8443"
    )


async def test_static_bearer_is_compared_in_constant_time(monkeypatch):
    monkeypatch.setenv("ARGUS_TOKEN", "s3cret-token")
    monkeypatch.delenv("ARGUS_JWT_JWKS_URI", raising=False)
    verifier = server._build_auth()
    assert (await verifier.verify_token("s3cret-token")).client_id == "argus"
    assert await verifier.verify_token("s3cret-tokeX") is None
    assert await verifier.verify_token("") is None


async def test_screenshot_reaches_the_client_as_an_image(app_state):
    out = await server.mcp.call_tool("screenshot", {"url": "http://fixtures.test/article"})
    kinds = [c.type for c in out.content]
    assert kinds == ["text", "image"]
    assert out.content[1].mime_type == "image/png" and out.content[1].data == "BASE64PNG"
    assert "screenshot" not in out.structured_content
    assert out.structured_content["format"] == "png"


async def test_dns_failure_is_reported_as_dns_failed(app_state, monkeypatch):
    from argus.security.ssrf import DNSError

    async def no_dns(*a, **k):
        raise DNSError("resolution timed out for 'pasal.id'")

    monkeypatch.setattr(server, "fetch", no_dns)
    out = await server.mcp.call_tool("read", {"url": "https://pasal.id/x"})
    assert out.is_error and out.structured_content["code"] == "dns_failed"


async def test_research_ctx_is_not_part_of_the_schema():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert "ctx" not in tools["research"].parameters["properties"]
    assert tools["search"].parameters["properties"]["category"]["enum"] == [
        "general", "news", "science", "it"]


def test_package_logger_reaches_stderr_at_info():
    """uvicorn leaves the root logger without a handler; argus.* INFO (fallback ladder,
    egress refusals) must still reach the container log."""
    import logging

    pkg = logging.getLogger("argus")
    assert pkg.isEnabledFor(logging.INFO)
    assert any(type(h) is logging.StreamHandler for h in pkg.handlers)


async def _probe_with(monkeypatch, handler):
    """Run server._egress_ok with the proxied client answering through ``handler``."""
    import httpx

    from argus import config
    from argus.fetch import static

    calls = []

    def h(req):
        calls.append(str(req.url))
        return handler(req)

    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")
    monkeypatch.setattr(server, "_egress_probe", (float("-inf"), False))
    monkeypatch.setattr(static, "_proxied_client",
                        lambda via: httpx.AsyncClient(transport=httpx.MockTransport(h)))
    return await server._egress_ok(), calls


async def test_egress_probe_ok_only_when_warp_is_on(monkeypatch):
    import httpx

    ok, calls = await _probe_with(
        monkeypatch, lambda r: httpx.Response(200, text="ip=1\nwarp=on\n"))
    assert ok is True and calls == ["https://www.cloudflare.com/cdn-cgi/trace"]
    off, _ = await _probe_with(monkeypatch, lambda r: httpx.Response(200, text="warp=off\n"))
    assert off is False  # reachable but not through WARP (e.g. registration broke)

    def down(req):
        raise httpx.ProxyError("warp unreachable")

    dead, _ = await _probe_with(monkeypatch, down)
    assert dead is False


async def test_egress_probe_is_cached_and_off_when_unconfigured(monkeypatch):
    import httpx

    from argus import config

    ok, calls = await _probe_with(monkeypatch, lambda r: httpx.Response(200, text="warp=on"))
    assert ok is True
    assert await server._egress_ok() is True and len(calls) == 1  # cached, no second probe
    monkeypatch.setattr(config, "EGRESS_PROXY", "")
    assert await server._egress_ok() is None


async def test_health_reports_egress_but_stays_200(app_state, monkeypatch):
    """A broken WARP must alert (uptime.yml greps "egress":false) without making Argus
    unhealthy: Traefik would then stop routing to it over an optional dependency."""
    app_state.browser._crawler = object()

    async def down():
        return False

    monkeypatch.setattr(server, "_egress_ok", down)
    public = await server.health(SimpleNamespace(client=SimpleNamespace(host="10.11.0.9")))
    assert public.status_code == 200
    assert b'"status":"ok"' in bytes(public.body) and b'"egress":false' in bytes(public.body)

    async def off():
        return None

    monkeypatch.setattr(server, "_egress_ok", off)
    body = bytes((await server.health(SimpleNamespace(client=None))).body)
    assert b"egress" not in body


_REPORT = {"at": "2026-10-04T00:00:00+00:00", "sites": {
    "reuters.com": {"direct": 401, "warp": 403},
    "example.com": {"direct": 200, "warp": 200}}}


async def test_reach_loop_measures_both_paths_and_survives_errors(app_state, monkeypatch):
    from argus import config

    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")
    monkeypatch.setattr(config, "EGRESS_PROXY_HOSTS", ("reuters.com",))
    monkeypatch.setattr(server, "_REACH", None)
    monkeypatch.setattr(server, "_REACH_PREV", None)
    seen, sleeps = [], []

    async def measure(direct, proxied=None):
        seen.append((direct, proxied is not None))
        if len(seen) == 1:
            raise RuntimeError("network hiccup")
        return _REPORT

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(server.reachability, "measure", measure)
    monkeypatch.setattr(server.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await server._reach_loop()
    # failed run -> daily; a run with a failing listed host -> re-measure within the hour
    assert sleeps == [server.REACH_FIRST_S, server.REACH_EVERY_S, server.REACH_RETRY_S]
    assert seen == [(app_state.client, True), (app_state.client, True)]
    assert server._REACH == _REPORT


async def test_health_and_metrics_surface_the_reachability_map(app_state, monkeypatch):
    from argus import config

    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")
    monkeypatch.setattr(config, "EGRESS_PROXY_HOSTS", ("reuters.com",))
    monkeypatch.setattr(server, "_REACH", _REPORT)
    monkeypatch.setattr(server, "_REACH_PREV", _REPORT)  # failing twice in a row

    async def up():
        return True

    monkeypatch.setattr(server, "_egress_ok", up)
    app_state.browser._crawler = object()
    app_state.browser.active_contexts = 0
    public = bytes((await server.health(SimpleNamespace(
        client=SimpleNamespace(host="10.11.0.9")))).body)
    assert b'"egress_hosts_failing":["reuters.com"]' in public and b"example.com" not in public
    local = bytes((await server.health(SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1")))).body)
    assert b'"reachability"' in local and b'"example.com"' in local
    metrics = bytes((await server.metrics(SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1")))).body)
    assert b'argus_reach_ok{site="reuters.com",via="warp"} 0' in metrics
    assert b'argus_reach_ok{site="example.com",via="direct"} 1' in metrics


async def test_one_failing_run_does_not_alert(app_state, monkeypatch):
    from argus import config

    monkeypatch.setattr(config, "EGRESS_PROXY", "http://warp:9091")
    monkeypatch.setattr(config, "EGRESS_PROXY_HOSTS", ("reuters.com",))
    monkeypatch.setattr(server, "_REACH", _REPORT)
    monkeypatch.setattr(server, "_REACH_PREV", None)  # e.g. right after a restart

    async def up():
        return True

    monkeypatch.setattr(server, "_egress_ok", up)
    app_state.browser._crawler = object()
    public = bytes((await server.health(SimpleNamespace(
        client=SimpleNamespace(host="10.11.0.9")))).body)
    assert b"egress_hosts_failing" not in public


async def test_scrape_landing_on_internal_host_reports_ssrf_blocked(app_state):
    """Forced browser (scrape): the client hears ssrf_blocked, which it does not retry."""
    from argus.security.ssrf import SSRFError

    async def render(*a, **k):
        raise SSRFError("blocked IP for 'searxng': 172.20.0.2")

    app_state.browser.render = render
    out = await server.scrape("https://example.com/redirect")
    assert out["code"] == "ssrf_blocked"
