import json

import pytest

from argus.cache import DEFAULT_TTLS, Cache, ttl_for


@pytest.fixture
def cache(tmp_path):
    c = Cache(db_path=str(tmp_path / "cache.db"), blob_dir=str(tmp_path / "blobs"))
    yield c
    c.close()


# 1. key determinism
def test_key_insertion_order_independent(cache):
    k1 = cache.key("https://example.com", {"a": 1, "b": 2})
    k2 = cache.key("https://example.com", {"b": 2, "a": 1})
    assert k1 == k2


def test_key_canonicalizes_url(cache):
    assert cache.key("  HTTPS://Example.com  ", {}) == cache.key(
        "https://example.com", {}
    )


def test_key_differs_on_url(cache):
    assert cache.key("https://a.com", {}) != cache.key("https://b.com", {})


def test_key_differs_on_opts(cache):
    assert cache.key("https://a.com", {"x": 1}) != cache.key("https://a.com", {"x": 2})


# 2. put/get within ttl, expiry, missing
def test_put_then_get_within_ttl(cache):
    k = cache.key("https://a.com", {})
    payload = {"content": "hello", "n": 7}
    cache.put(k, payload, source="read")
    assert cache.get(k, ttl_seconds=3600) == payload


def test_get_expired_returns_none(cache):
    k = cache.key("https://a.com", {})
    cache.put(k, {"content": "x"}, source="read")
    assert cache.get(k, ttl_seconds=0) is None


def test_get_expired_via_monkeypatched_time(cache, monkeypatch):
    k = cache.key("https://a.com", {})
    cache.put(k, {"content": "x"}, source="read")
    import argus.cache as cache_mod

    real = cache_mod.time.time()
    monkeypatch.setattr(cache_mod.time, "time", lambda: real + 1000)
    assert cache.get(k, ttl_seconds=600) is None
    assert cache.get(k, ttl_seconds=2000) == {"content": "x"}


def test_get_missing_returns_none(cache):
    assert cache.get("nonexistent-key", ttl_seconds=3600) is None


# 3. get_stale
def test_get_stale_returns_expired_payload(cache, monkeypatch):
    k = cache.key("https://a.com", {})
    cache.put(k, {"content": "stale-ok"}, source="read")
    import argus.cache as cache_mod

    real = cache_mod.time.time()
    monkeypatch.setattr(cache_mod.time, "time", lambda: real + 10**9)
    assert cache.get(k, ttl_seconds=600) is None
    assert cache.get_stale(k) == {"content": "stale-ok"}


def test_get_stale_missing_returns_none(cache):
    assert cache.get_stale("nope") is None


# 4. large payload via blob, small inline
def test_large_payload_roundtrip_via_blob(cache, tmp_path):
    blob_dir = tmp_path / "blobs"
    k = cache.key("https://big.com", {})
    payload = {"content": "z" * 40000}
    assert len(json.dumps(payload)) > 32768
    cache.put(k, payload, source="read")
    files = list(blob_dir.glob("*"))
    assert len(files) == 1
    assert cache.get(k, ttl_seconds=3600) == payload


def test_small_payload_stays_inline(cache, tmp_path):
    blob_dir = tmp_path / "blobs"
    k = cache.key("https://small.com", {})
    cache.put(k, {"content": "tiny"}, source="read")
    # no blob file written for inline storage
    assert list(blob_dir.glob("*")) == []
    # verify blob_path column is NULL
    row = cache.conn.execute(
        "SELECT blob_path FROM entries WHERE key=?", (k,)
    ).fetchone()
    assert row[0] is None
    assert cache.get(k, ttl_seconds=3600) == {"content": "tiny"}


# 5. upsert
def test_upsert_replaces_and_keeps_one_row(cache):
    k = cache.key("https://a.com", {})
    cache.put(k, {"v": 1}, source="read")
    cache.put(k, {"v": 2}, source="read")
    assert cache.get(k, ttl_seconds=3600) == {"v": 2}
    count = cache.conn.execute(
        "SELECT COUNT(*) FROM entries WHERE key=?", (k,)
    ).fetchone()[0]
    assert count == 1


# 6. ttl_for
def test_ttl_for_known():
    assert ttl_for("news") == 900
    assert ttl_for("trading") == 300
    assert ttl_for("docs") == 86400


def test_ttl_for_unknown_falls_back_to_general():
    assert ttl_for("unknown") == 3600
    assert ttl_for("unknown") == DEFAULT_TTLS["general"]
