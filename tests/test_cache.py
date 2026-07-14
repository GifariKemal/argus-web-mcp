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


# 5b. WAL + busy_timeout pragmas (audit C2: concurrent-access resilience)
def test_wal_and_busy_timeout_pragmas(cache):
    journal = cache.conn.execute("PRAGMA journal_mode").fetchone()[0]
    # a file-backed db reports 'wal'; an in-memory db can only report 'memory'
    assert journal == "wal"
    busy = cache.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy == 5000


# 6. ttl_for
def test_ttl_for_known():
    assert ttl_for("news") == 900
    assert ttl_for("trading") == 300
    assert ttl_for("docs") == 86400


def test_ttl_for_unknown_falls_back_to_general():
    assert ttl_for("unknown") == 3600
    assert ttl_for("unknown") == DEFAULT_TTLS["general"]


# 4. path/query case-sensitivity (RFC 3986: only scheme+host are case-insensitive)
def test_key_path_case_sensitive(cache):
    assert cache.key("https://a.com/X", {}) != cache.key("https://a.com/x", {})
    assert cache.key("https://a.com/p?Q=A", {}) != cache.key("https://a.com/p?q=a", {})


# 5. missing/corrupt blob self-heals as a cache miss (never raises into a tool)
def test_missing_blob_is_cache_miss_and_row_deleted(cache):
    k = cache.key("https://big.com", {})
    cache.put(k, {"content": "y" * 40000}, source="read")
    blob = cache.blob_dir / k
    assert blob.exists()
    blob.unlink()
    assert cache.get(k, ttl_seconds=3600) is None
    row = cache.conn.execute("SELECT 1 FROM entries WHERE key=?", (k,)).fetchone()
    assert row is None  # self-healed: dead row removed


def test_corrupt_blob_is_stale_miss(cache):
    k = cache.key("https://big2.com", {})
    cache.put(k, {"content": "y" * 40000}, source="read")
    (cache.blob_dir / k).write_text("{not json", encoding="utf-8")
    assert cache.get_stale(k) is None
    # self-heal must also delete the orphaned blob file, not just the DB row
    assert not (cache.blob_dir / k).exists()


# 6. purge: old rows + blobs deleted, fresh entries survive
def test_purge_removes_old_rows_and_blobs(cache, monkeypatch):
    import argus.cache as cache_mod

    real = cache_mod.time.time()
    monkeypatch.setattr(cache_mod.time, "time", lambda: real - 8 * 86400)
    old_inline = cache.key("https://old-inline.com", {})
    cache.put(old_inline, {"content": "x"}, source="read")
    old_blob = cache.key("https://old-blob.com", {})
    cache.put(old_blob, {"content": "y" * 40000}, source="read")
    blob_file = cache.blob_dir / old_blob
    # blob file mtime must also predate the cutoff for the orphan sweep
    import os

    os.utime(blob_file, (real - 8 * 86400, real - 8 * 86400))
    monkeypatch.setattr(cache_mod.time, "time", lambda: real)
    fresh = cache.key("https://fresh.com", {})
    cache.put(fresh, {"content": "z"}, source="read")

    deleted = cache.purge()
    assert deleted == 2
    assert cache.get_stale(old_inline) is None
    assert cache.get_stale(old_blob) is None
    assert not blob_file.exists()
    assert cache.get_stale(fresh) == {"content": "z"}


def test_purge_removes_orphaned_blob_files(cache, monkeypatch):
    import os

    import argus.cache as cache_mod

    real = cache_mod.time.time()
    k = cache.key("https://shrink.com", {})
    cache.put(k, {"content": "y" * 40000}, source="read")  # blob tier
    blob_file = cache.blob_dir / k
    cache.put(k, {"content": "small"}, source="read")  # re-put inline -> blob orphaned
    assert blob_file.exists()
    os.utime(blob_file, (real - 8 * 86400, real - 8 * 86400))
    cache.purge()
    assert not blob_file.exists()
    assert cache.get_stale(k) == {"content": "small"}  # live inline row untouched


def test_purge_leaves_fresh_blob_files(cache):
    """The orphan sweep must skip blob files younger than the cutoff."""
    k = cache.key("https://fresh-blob.com", {})
    cache.put(k, {"content": "y" * 40000}, source="read")
    blob = cache.blob_dir / k
    assert blob.exists()
    assert cache.purge() == 0
    assert blob.exists()
    assert cache.get_stale(k) == {"content": "y" * 40000}


# 7. blob compression (0.4.7): blobs are gzip on disk; legacy plain blobs still read.
def test_blob_is_gzip_compressed_on_disk(cache):
    k = cache.key("https://big.com", {})
    payload = {"content": "z" * 40000}  # highly compressible
    cache.put(k, payload, source="read")
    blob = cache.blob_dir / k
    raw = blob.read_bytes()
    assert raw[:2] == b"\x1f\x8b"  # gzip magic
    assert len(raw) < len(json.dumps(payload).encode("utf-8"))  # actually smaller
    assert cache.get(k, ttl_seconds=3600) == payload  # round-trips


def test_legacy_plaintext_blob_still_reads(cache):
    """A blob written before 0.4.7 (plain UTF-8 JSON) must still decode."""
    k = cache.key("https://legacy.com", {})
    payload = {"content": "y" * 40000}
    blob = cache.blob_dir / k
    blob.write_text(json.dumps(payload), encoding="utf-8")  # legacy uncompressed
    cache.conn.execute(
        "INSERT OR REPLACE INTO entries (key, payload, blob_path, source, created) "
        "VALUES (?, NULL, ?, ?, ?)",
        (k, str(blob), "read", __import__("time").time()),
    )
    cache.conn.commit()
    assert cache.get(k, ttl_seconds=3600) == payload
