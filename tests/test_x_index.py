"""X module, Layer 0: x_search over the web index with FxTwitter enrichment (offline)."""

import asyncio
from datetime import date

import pytest

from argus import server
from argus.search import SearchError
from argus.x import index
from argus.x.index import search_posts


def _post(pid: str, created: str = "2026-10-01T00:00:00Z") -> dict:
    return {"id": pid, "url": f"https://x.com/u/status/{pid}", "created_at": created,
            "text": f"post {pid}", "source": "fxtwitter"}


class FakeSearch:
    def __init__(self, urls: list[str]):
        self.urls = urls
        self.calls = 0
        self.query = None
        self.kwargs: dict = {}

    async def __call__(self, query, **kwargs):
        self.calls += 1
        self.query, self.kwargs = query, kwargs
        return {"results": [{"url": u, "title": "t", "snippet": "s"} for u in self.urls]}


@pytest.fixture
def fx(monkeypatch):
    """fx_post stub: a post per id unless listed in ``missing``; tracks concurrency."""
    state = {"calls": [], "in_flight": 0, "peak": 0, "missing": set(), "created": {}}

    async def fake_fx_post(pid, *, client, timeout=30):
        state["calls"].append(pid)
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0.01)
        state["in_flight"] -= 1
        if pid in state["missing"]:
            return None
        return _post(pid, state["created"].get(pid, "2026-10-01T00:00:00Z"))

    monkeypatch.setattr(index, "fx_post", fake_fx_post)
    return state


async def _run(search, **kw):
    args = {"mode": "top", "limit": 10, "since": None, "until": None} | kw
    return await search_posts("esp32 modbus", search_fn=search, client=None, **args)


async def test_x_search_filters_dedupes_and_enriches(fx):
    search = FakeSearch([
        "https://x.com/a/status/111", "https://twitter.com/a/status/111?s=20",
        "https://x.com/someone", "https://en.wikipedia.org/wiki/ESP32",
        "https://x.com/b/status/222/photo/1",
    ])
    res = await _run(search)
    assert [p["id"] for p in res["results"]] == ["111", "222"]
    assert res["source"] == "index" and res["degraded"] is False
    assert res["degraded_reason"] is None
    assert search.query == "esp32 modbus"
    assert search.kwargs["include_domains"] == ["x.com", "twitter.com"]
    assert search.kwargs["count"] == 30


async def test_x_search_latest_sorts_newest_first_and_flags_index_only(fx):
    fx["created"] = {"1": "2026-01-01T00:00:00Z", "2": "2026-10-01T00:00:00Z",
                     "3": "2026-05-01T00:00:00Z"}
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in "123"]), mode="latest")
    assert [p["id"] for p in res["results"]] == ["2", "3", "1"]
    assert res["degraded"] is True and res["degraded_reason"] == "index_only"


async def test_x_search_drops_unenrichable_ids(fx):
    fx["missing"] = {"2"}
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in "123"]))
    assert [p["id"] for p in res["results"]] == ["1", "3"]


async def test_x_search_caps_at_limit_and_concurrency(fx):
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in range(1, 31)]), limit=5)
    assert len(fx["calls"]) == 5 and len(res["results"]) == 5
    assert fx["peak"] <= 4


async def test_x_search_date_window(fx):
    fx["created"] = {"1": "2026-09-30T23:59:59Z", "2": "2026-10-01T00:00:00Z",
                     "3": "2026-10-05T23:59:59Z", "4": "2026-10-06T00:00:00Z"}
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in "1234"]),
                     since=date(2026, 10, 1), until=date(2026, 10, 5))
    assert [p["id"] for p in res["results"]] == ["2", "3"]


# --- tool boundary ---------------------------------------------------------------

@pytest.mark.parametrize(("since", "until"), [
    ("2026-02-30", None),
    ("2026-10-30", "2026-10-01"),
    ("\uff12\uff10\uff12\uff16-10-01", None),
    ("30-10-2026", None),
    (None, "2026-13-01"),
])
async def test_x_search_rejects_bad_dates(app_state, since, until):
    r = await server.x_search("esp32", since=since, until=until)
    assert r["code"] == "schema_invalid"


async def test_x_search_rejects_bad_mode(app_state):
    r = await server.x_search("esp32", mode="oldest")
    assert r["code"] == "schema_invalid"


async def test_x_search_tool_caches_per_mode(app_state, monkeypatch, fx):
    search = FakeSearch(["https://x.com/u/status/1"])
    monkeypatch.setattr(server, "searxng_search", search)
    r1 = await server.x_search("esp32", limit=500)
    assert r1["from_cache"] is False and r1["count"] == 1 and r1["mode"] == "top"
    assert search.kwargs["count"] == 60  # limit clamped to 50, three candidates each
    r2 = await server.x_search("esp32", limit=500)
    assert r2["from_cache"] is True and search.calls == 1
    r3 = await server.x_search("esp32", mode="latest", limit=500)
    assert r3["from_cache"] is False and search.calls == 2


async def test_x_search_maps_backend_errors(app_state, monkeypatch, fx):
    async def no_results(query, **kw):
        raise SearchError("no_results", "none")

    async def down(query, **kw):
        raise SearchError("search_backend_down", "engines dead")

    monkeypatch.setattr(server, "searxng_search", no_results)
    assert (await server.x_search("esp32"))["code"] == "no_results"
    monkeypatch.setattr(server, "searxng_search", down)
    assert (await server.x_search("esp32 b"))["code"] == "search_backend_down"
    monkeypatch.setattr(server, "searxng_search", FakeSearch(["https://en.wikipedia.org/x"]))
    assert (await server.x_search("esp32 c"))["code"] == "no_results"


# --- final-review fixes ----------------------------------------------------------

def sf(day: str) -> str:
    """A real X snowflake id for 12:00Z on ``day`` (YYYY-MM-DD)."""
    from datetime import UTC, datetime
    ms = int(datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=UTC).timestamp() * 1000)
    return str((ms - 1288834974657) << 22)


async def test_x_search_enrich_exception_drops_only_that_id(monkeypatch):
    async def flaky(pid, *, client, timeout=30):
        if pid == "2":
            raise TypeError("malformed upstream shape")
        return _post(pid)

    monkeypatch.setattr(index, "fx_post", flaky)
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in "123"]))
    assert [p["id"] for p in res["results"]] == ["1", "3"]


async def test_x_search_date_window_uses_snowflakes_before_enrichment(fx):
    ids = [sf("2026-09-01"), sf("2026-09-02"), sf("2026-10-26"), sf("2026-09-03"),
           sf("2026-10-27")]
    fx["created"] = {ids[2]: "2026-10-26T12:00:00Z", ids[4]: "2026-10-27T12:00:00Z"}
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in ids]), limit=2,
                     since=date(2026, 10, 25))
    assert [p["id"] for p in res["results"]] == [ids[2], ids[4]]
    assert fx["calls"] == [ids[2], ids[4]]  # out-of-window ids never cost a request


async def test_x_search_tops_up_when_posts_are_gone(fx):
    fx["missing"] = {"1"}
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in "123"]), limit=2)
    assert [p["id"] for p in res["results"]] == ["2", "3"]


async def test_x_search_latest_prefers_newest_candidates(fx):
    old, new = sf("2025-01-01"), sf("2026-10-01")
    fx["created"] = {old: "2025-01-01T12:00:00Z", new: "2026-10-01T12:00:00Z"}
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in (old, new)]),
                     mode="latest", limit=1)
    assert [p["id"] for p in res["results"]] == [new]


async def test_x_search_enrichment_has_a_wall_clock(monkeypatch):
    async def hang(pid, *, client, timeout=30):
        if pid == "2":
            await asyncio.sleep(5)
        return _post(pid)

    monkeypatch.setattr(index, "fx_post", hang)
    monkeypatch.setattr(index, "ENRICH_BUDGET_S", 0.2)
    res = await _run(FakeSearch([f"https://x.com/u/status/{i}" for i in "12"]))
    assert [p["id"] for p in res["results"]] == ["1"]
    assert res["degraded"] is True and res["degraded_reason"] == "enrich_timeout"


async def test_x_search_passes_fx_timeout(fx, monkeypatch):
    seen = []

    async def rec(pid, *, client, timeout=30):
        seen.append(timeout)
        return _post(pid)

    monkeypatch.setattr(index, "fx_post", rec)
    await _run(FakeSearch(["https://x.com/u/status/1"]))
    assert seen == [index.FX_TIMEOUT_S] and index.FX_TIMEOUT_S <= 10


async def test_x_search_surfaces_search_degradation_and_skips_cache(app_state, monkeypatch, fx):
    class Degraded(FakeSearch):
        async def __call__(self, query, **kwargs):
            res = await super().__call__(query, **kwargs)
            return {**res, "degraded": True, "degraded_reason": "backend_failover"}

    search = Degraded(["https://x.com/u/status/1"])
    monkeypatch.setattr(server, "searxng_search", search)
    r1 = await server.x_search("esp32 degraded")
    assert r1["degraded"] is True and r1["degraded_reason"] == "backend_failover"
    r2 = await server.x_search("esp32 degraded")
    assert r2["from_cache"] is False and search.calls == 2


async def test_x_search_partial_enrichment_is_not_cached(app_state, monkeypatch, fx):
    fx["missing"] = {"2", "3", "4"}
    search = FakeSearch([f"https://x.com/u/status/{i}" for i in "1234"])
    monkeypatch.setattr(server, "searxng_search", search)
    r1 = await server.x_search("esp32 partial", limit=4)
    assert r1["degraded_reason"] == "partial_enrichment"
    await server.x_search("esp32 partial", limit=4)
    assert search.calls == 2


def test_x_search_ties_x_operators_to_the_account():
    """The index tier ignores X operators; the description may only offer them together
    with the condition that makes them work (the server's X account, source='graphql')."""
    import typing
    doc = server.x_search.__doc__ or ""
    query_desc = typing.get_type_hints(
        server.x_search, include_extras=True)["query"].__metadata__[0].description
    assert "from:" not in doc and "X search syntax" not in doc + query_desc
    assert "from:" in query_desc and "work only when the server has an X account" in query_desc
    assert "keyword" in doc.lower() and "keyword" in query_desc.lower()
