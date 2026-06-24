"""Tests for argus.watch - register/diff/notify watch subsystem.

Fully offline: a fake async fetch_fn returns canned content, a fake httpx-like
client records POSTs, the clock is an explicit ``now`` float. The webhook SSRF
guard is exercised by monkeypatching ``socket.getaddrinfo`` to a private IP.
"""

import socket

import pytest

from argus import watch as W
from argus.watch import (
    Watch,
    WatchStore,
    check_watch,
    content_signature,
    deliver,
    poll_due,
)

CHANGELOG_HTML = "<html><body><h1>v1</h1><p>old release</p></body></html>"
CHANGELOG_HTML2 = "<html><body><h1>v2</h1><p>new release</p></body></html>"


def _store(tmp_path):
    return WatchStore(path=str(tmp_path / "watches.json"))


class FakeFetch:
    """Async fetch_fn stub: returns a configured payload per call, or raises."""

    def __init__(self, payload=None, *, error=None, key="html"):
        self.payload = payload
        self.error = error
        self.key = key
        self.calls: list[str] = []
        self.responses: dict[str, object] = {}

    async def __call__(self, url):
        self.calls.append(url)
        if url in self.responses:
            r = self.responses[url]
            if isinstance(r, Exception):
                raise r
            return r
        if self.error is not None:
            raise self.error
        return {self.key: self.payload}


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeClient:
    """httpx-like async client recording POSTs; status_code from a queue."""

    def __init__(self, status_code=200, *, raise_on_post=None):
        self.status_code = status_code
        self.raise_on_post = raise_on_post
        self.posts: list[dict] = []

    async def post(self, url, *, json=None, **kwargs):
        self.posts.append({"url": url, "json": json})
        if self.raise_on_post is not None:
            raise self.raise_on_post
        return FakeResponse(self.status_code)


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every hostname to a public IP so the webhook SSRF guard allows it."""

    def _gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _gai)


@pytest.fixture
def private_dns(monkeypatch):
    """Resolve every hostname to a private IP so the webhook SSRF guard blocks it."""

    def _gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _gai)


# --------------------------------------------------------------------------- #
# WatchStore
# --------------------------------------------------------------------------- #


def test_store_add_list_roundtrip(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    assert isinstance(w, Watch)
    assert w.url == "https://x.test/cal"
    assert w.interval_s == 300
    assert w.webhook == "https://hook.test/in"
    assert w.last_hash is None and w.last_check is None
    assert s.list() == [w]


def test_store_add_dedups_by_id(tmp_path):
    s = _store(tmp_path)
    a = s.add("https://x.test/cal", "h1", 300, "https://hook.test/in")
    b = s.add("https://x.test/cal", "h1", 999, "https://hook.test/in")
    assert a.id == b.id
    assert len(s.list()) == 1


def test_store_distinct_ids_for_distinct_specs(tmp_path):
    s = _store(tmp_path)
    a = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    b = s.add("https://x.test/cal", ".price", 300, "https://hook.test/in")
    c = s.add("https://x.test/cal", None, 300, "https://other.test/in")
    assert len({a.id, b.id, c.id}) == 3


def test_store_remove(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    assert s.remove(w.id) is True
    assert s.list() == []
    assert s.remove(w.id) is False
    assert s.remove("nope") is False


def test_store_update_state(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    s.update_state(w.id, "deadbeef", 1234.5)
    got = s.list()[0]
    assert got.last_hash == "deadbeef"
    assert got.last_check == 1234.5


def test_store_persists_across_instances(tmp_path):
    path = str(tmp_path / "watches.json")
    s1 = WatchStore(path=path)
    w = s1.add("https://x.test/cal", "h1", 300, "https://hook.test/in")
    s1.update_state(w.id, "abc123", 999.0)

    s2 = WatchStore(path=path)
    got = s2.list()
    assert len(got) == 1
    assert got[0].id == w.id
    assert got[0].last_hash == "abc123"
    assert got[0].last_check == 999.0
    assert got[0].selector == "h1"


def test_store_empty_when_no_file(tmp_path):
    s = _store(tmp_path)
    assert s.list() == []


def test_store_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    monkeypatch.setenv("HOME", str(tmp_path))  # POSIX
    s = WatchStore(path="~/.argus/watches.json")
    w = s.add("https://x.test/cal", None, 60, "https://hook.test/in")
    assert (tmp_path / ".argus" / "watches.json").exists()
    assert s.list() == [w]


# --------------------------------------------------------------------------- #
# content_signature
# --------------------------------------------------------------------------- #


def test_signature_stable_same_input():
    assert content_signature("hello") == content_signature("hello")


def test_signature_differs_on_change():
    assert content_signature("hello") != content_signature("world")


def test_signature_uses_selector_value_when_given():
    # Same full text, different selector value -> different signatures.
    assert content_signature("FULL", "v1") != content_signature("FULL", "v2")
    # Selector value drives the hash, ignoring full text.
    assert content_signature("A", "same") == content_signature("B", "same")


# --------------------------------------------------------------------------- #
# check_watch
# --------------------------------------------------------------------------- #


async def test_check_first_time_baseline_not_changed(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    fetch = FakeFetch(CHANGELOG_HTML)
    res = await check_watch(w, fetch_fn=fetch, now=100.0)
    assert res["changed"] is False
    assert res["new_hash"] == content_signature(CHANGELOG_HTML)


async def test_check_unchanged_second_time(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    base = content_signature(CHANGELOG_HTML)
    w.last_hash = base
    fetch = FakeFetch(CHANGELOG_HTML)
    res = await check_watch(w, fetch_fn=fetch, now=200.0)
    assert res["changed"] is False
    assert res["new_hash"] == base


async def test_check_changed(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    w.last_hash = content_signature(CHANGELOG_HTML)
    fetch = FakeFetch(CHANGELOG_HTML2)
    res = await check_watch(w, fetch_fn=fetch, now=300.0)
    assert res["changed"] is True
    assert res["new_hash"] == content_signature(CHANGELOG_HTML2)


async def test_check_selector_extracts_value(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", "h1", 300, "https://hook.test/in")
    fetch = FakeFetch(CHANGELOG_HTML)
    res = await check_watch(w, fetch_fn=fetch, now=100.0)
    assert res["value"] == "v1"
    assert res["new_hash"] == content_signature(CHANGELOG_HTML, "v1")


async def test_check_selector_change_detected(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", "h1", 300, "https://hook.test/in")
    w.last_hash = content_signature(CHANGELOG_HTML, "v1")
    fetch = FakeFetch(CHANGELOG_HTML2)
    res = await check_watch(w, fetch_fn=fetch, now=300.0)
    assert res["value"] == "v2"
    assert res["changed"] is True


async def test_check_fetch_error_never_raises(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    fetch = FakeFetch(error=RuntimeError("boom"))
    res = await check_watch(w, fetch_fn=fetch, now=100.0)
    assert res["changed"] is False
    assert "error" in res


async def test_check_reads_content_key(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    fetch = FakeFetch("plain markdown body", key="content")
    res = await check_watch(w, fetch_fn=fetch, now=100.0)
    assert res["new_hash"] == content_signature("plain markdown body")


# --------------------------------------------------------------------------- #
# deliver
# --------------------------------------------------------------------------- #


async def test_deliver_2xx_true(public_dns):
    client = FakeClient(status_code=200)
    ok = await deliver("https://hook.test/in", {"k": "v"}, client=client)
    assert ok is True
    assert client.posts == [{"url": "https://hook.test/in", "json": {"k": "v"}}]


async def test_deliver_non_2xx_false(public_dns):
    client = FakeClient(status_code=500)
    ok = await deliver("https://hook.test/in", {"k": "v"}, client=client)
    assert ok is False
    assert len(client.posts) == 1  # attempted


async def test_deliver_ssrf_blocked_not_posted(private_dns):
    client = FakeClient(status_code=200)
    ok = await deliver("https://hook.internal/in", {"k": "v"}, client=client)
    assert ok is False
    assert client.posts == []  # NEVER posted to a blocked URL


async def test_deliver_bad_scheme_not_posted():
    client = FakeClient(status_code=200)
    ok = await deliver("file:///etc/passwd", {"k": "v"}, client=client)
    assert ok is False
    assert client.posts == []


async def test_deliver_client_exception_returns_false(public_dns):
    client = FakeClient(raise_on_post=httpx_error())
    ok = await deliver("https://hook.test/in", {"k": "v"}, client=client)
    assert ok is False


def httpx_error():
    return ConnectionError("network down")


# --------------------------------------------------------------------------- #
# poll_due
# --------------------------------------------------------------------------- #


async def test_poll_due_changed_delivers_and_updates(tmp_path, public_dns):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    s.update_state(w.id, content_signature(CHANGELOG_HTML), 0.0)
    fetch = FakeFetch(CHANGELOG_HTML2)
    client = FakeClient(status_code=200)

    results = await poll_due(s, fetch_fn=fetch, client=client, now=1000.0)

    assert len(results) == 1
    assert results[0]["changed"] is True
    assert results[0]["delivered"] is True
    assert len(client.posts) == 1
    body = client.posts[0]["json"]
    assert body["watch_id"] == w.id
    assert body["url"] == "https://x.test/cal"
    updated = s.list()[0]
    assert updated.last_hash == content_signature(CHANGELOG_HTML2)
    assert updated.last_check == 1000.0


async def test_poll_due_skips_not_due(tmp_path, public_dns):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    s.update_state(w.id, content_signature(CHANGELOG_HTML), 900.0)
    fetch = FakeFetch(CHANGELOG_HTML2)
    client = FakeClient(status_code=200)

    # now - last_check = 100 < interval 300 -> skipped
    results = await poll_due(s, fetch_fn=fetch, client=client, now=1000.0)

    assert results == []
    assert fetch.calls == []
    assert client.posts == []
    assert s.list()[0].last_check == 900.0  # untouched


async def test_poll_due_baseline_first_poll_no_deliver(tmp_path, public_dns):
    s = _store(tmp_path)
    s.add("https://x.test/cal", None, 300, "https://hook.test/in")
    fetch = FakeFetch(CHANGELOG_HTML)
    client = FakeClient(status_code=200)

    results = await poll_due(s, fetch_fn=fetch, client=client, now=500.0)

    assert results[0]["changed"] is False
    assert results[0]["delivered"] is False
    assert client.posts == []  # baseline, no notification
    updated = s.list()[0]
    assert updated.last_hash == content_signature(CHANGELOG_HTML)
    assert updated.last_check == 500.0  # state set even on baseline


async def test_poll_due_resilient_one_failure(tmp_path, public_dns):
    s = _store(tmp_path)
    bad = s.add("https://bad.test/cal", None, 300, "https://hook.test/in")
    good = s.add("https://good.test/cal", None, 300, "https://hook.test/in")
    s.update_state(good.id, content_signature(CHANGELOG_HTML), 0.0)

    fetch = FakeFetch()
    fetch.responses["https://bad.test/cal"] = RuntimeError("boom")
    fetch.responses["https://good.test/cal"] = {"html": CHANGELOG_HTML2}
    client = FakeClient(status_code=200)

    results = await poll_due(s, fetch_fn=fetch, client=client, now=1000.0)

    assert len(results) == 2
    by_id = {r["id"]: r for r in results}
    assert by_id[bad.id]["changed"] is False  # errored, no crash
    assert by_id[good.id]["changed"] is True
    assert by_id[good.id]["delivered"] is True
    # good watch still delivered despite the bad one failing
    assert len(client.posts) == 1


async def test_poll_due_changed_but_delivery_fails(tmp_path, private_dns):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.internal/in")
    s.update_state(w.id, content_signature(CHANGELOG_HTML), 0.0)
    fetch = FakeFetch(CHANGELOG_HTML2)
    client = FakeClient(status_code=200)

    results = await poll_due(s, fetch_fn=fetch, client=client, now=1000.0)

    assert results[0]["changed"] is True
    assert results[0]["delivered"] is False  # SSRF-blocked webhook
    assert client.posts == []
    # State still advances so we don't re-alert forever on a broken webhook.
    assert s.list()[0].last_hash == content_signature(CHANGELOG_HTML2)


async def test_check_selector_no_match_value_none(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", ".missing", 300, "https://hook.test/in")
    fetch = FakeFetch(CHANGELOG_HTML)
    res = await check_watch(w, fetch_fn=fetch, now=100.0)
    assert res["value"] is None
    # value is None -> signature falls back to full content
    assert res["new_hash"] == content_signature(CHANGELOG_HTML)


async def test_check_fetch_returns_no_content_key(tmp_path):
    s = _store(tmp_path)
    w = s.add("https://x.test/cal", None, 300, "https://hook.test/in")

    async def _fetch(url):
        return {"final_url": url, "status": 200}  # no content/html/markdown key

    res = await check_watch(w, fetch_fn=_fetch, now=100.0)
    assert res["new_hash"] == content_signature("")


def test_store_update_state_missing_id_noop(tmp_path):
    s = _store(tmp_path)
    s.update_state("does-not-exist", "h", 1.0)  # must not raise
    assert s.list() == []


def test_store_ignores_unknown_keys_in_json(tmp_path):
    path = tmp_path / "watches.json"
    path.write_text(
        '[{"id":"abc","url":"https://x.test","selector":null,"interval_s":60,'
        '"webhook":"https://h.test","last_hash":null,"last_check":null,"stray":"x"}]',
        encoding="utf-8",
    )
    s = WatchStore(path=str(path))
    assert len(s.list()) == 1
    assert s.list()[0].id == "abc"


def test_store_corrupt_json_starts_empty(tmp_path):
    path = tmp_path / "watches.json"
    path.write_text("{ not json", encoding="utf-8")
    s = WatchStore(path=str(path))
    assert s.list() == []


def test_module_exports():
    for name in ("Watch", "WatchStore", "content_signature", "check_watch", "deliver", "poll_due"):
        assert hasattr(W, name)
