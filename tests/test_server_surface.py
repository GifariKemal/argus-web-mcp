"""The MCP-facing surface: what clients see in tools/list and in call results (0.4.20)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

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
