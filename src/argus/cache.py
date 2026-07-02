"""SQLite-backed response cache with per-category TTL and disk-blob overflow.

Stdlib only. Inline-JSON for small payloads, on-disk blob files for large ones.
Store-good-only: ``put`` is invoked exclusively by callers on upstream success,
so failures are never cached here. ``get_stale`` enables stale-serve fallback
when an upstream is transiently down.
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Per-category TTL seconds. Categories come from tools: news, docs, trading, pdf, search, general.
DEFAULT_TTLS = {
    "news": 900,
    "docs": 86400,
    "trading": 300,
    "pdf": 86400,
    "search": 600,
    "general": 3600,
}

_INLINE_LIMIT = 32768  # bytes; larger JSON payloads spill to a disk blob


def ttl_for(category: str) -> int:
    """Return TTL for a category, falling back to general's value (3600)."""
    return DEFAULT_TTLS.get(category, DEFAULT_TTLS["general"])


class Cache:
    def __init__(
        self, db_path: str = "~/.argus/cache.db", blob_dir: str = "~/.argus/blobs"
    ):
        self.db_path = Path(db_path).expanduser()
        self.blob_dir = Path(blob_dir).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # WAL + bounded lock waits: resilient to concurrent access if the deploy
        # ever goes multi-process (audit C2). put() still commits synchronously on
        # the loop; acceptable for current single-process use (ponytail: no threading).
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS entries ("
            "key TEXT PRIMARY KEY, payload TEXT NULL, blob_path TEXT NULL, "
            "source TEXT, created REAL)"
        )
        self.conn.commit()

    def key(self, url: str, opts: dict) -> str:
        """sha256 hex of canonical(url) + json.dumps(opts, sort_keys=True).

        Only scheme and host are case-insensitive per RFC 3986; path/query keep their
        case so `/API` and `/api` never collide onto the same cached content.
        """
        p = urlsplit(url.strip())
        canonical = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, p.fragment))
        material = canonical + json.dumps(opts, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _load(self, key: str, row: sqlite3.Row | tuple) -> dict | None:
        """Decode a row's payload. A missing/corrupt blob (deleted by disk cleanup, crash
        mid-write) must read as a cache MISS, not raise into the tool: self-heal by
        deleting the dead row and returning None so the caller refetches."""
        payload, blob_path = row[0], row[1]
        try:
            if blob_path is not None:
                return json.loads(Path(blob_path).read_text(encoding="utf-8"))
            return json.loads(payload)
        except (OSError, ValueError):
            self.conn.execute("DELETE FROM entries WHERE key=?", (key,))
            self.conn.commit()
            return None

    def get(self, key: str, ttl_seconds: int) -> dict | None:
        """Return stored payload if present AND fresh within ttl_seconds, else None."""
        row = self.conn.execute(
            "SELECT payload, blob_path, created FROM entries WHERE key=?", (key,)
        ).fetchone()
        if row is None or (time.time() - row[2]) >= ttl_seconds:
            return None
        return self._load(key, row)

    def get_stale(self, key: str) -> dict | None:
        """Return stored payload regardless of age, else None."""
        row = self.conn.execute(
            "SELECT payload, blob_path FROM entries WHERE key=?", (key,)
        ).fetchone()
        return self._load(key, row) if row is not None else None

    def purge(self, max_age_s: int = 7 * 86400) -> int:
        """Delete entries older than ``max_age_s`` (default 7 days, > every TTL so
        fresh-servable rows are never touched; keeps a generous stale-serve window).
        Unlinks their blob files, then sweeps ``blob_dir`` for orphans older than the
        cutoff (a re-put that shrank below the inline limit leaves its old blob file
        behind with no row referencing it). Returns the number of rows deleted."""
        cutoff = time.time() - max_age_s
        rows = self.conn.execute(
            "SELECT blob_path FROM entries WHERE created < ? AND blob_path IS NOT NULL",
            (cutoff,),
        ).fetchall()
        for (bp,) in rows:
            Path(bp).unlink(missing_ok=True)
        deleted = self.conn.execute(
            "DELETE FROM entries WHERE created < ?", (cutoff,)
        ).rowcount
        self.conn.commit()
        # Orphan sweep: put() rewrites a live blob on every upsert, so any file with
        # mtime older than the cutoff has no fresh row referencing it.
        for f in self.blob_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - racing deletes are fine
                continue
        return deleted

    def put(self, key: str, payload: dict, source: str) -> None:
        """Store payload (inline if small, disk-blob if > 32768 bytes). Upsert on key."""
        data = json.dumps(payload)
        if len(data.encode("utf-8")) > _INLINE_LIMIT:
            blob_path = self.blob_dir / key
            blob_path.write_text(data, encoding="utf-8")
            inline, blob = None, str(blob_path)
        else:
            inline, blob = data, None
        self.conn.execute(
            "INSERT OR REPLACE INTO entries (key, payload, blob_path, source, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, inline, blob, source, time.time()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


__all__ = ["DEFAULT_TTLS", "Cache", "ttl_for"]
