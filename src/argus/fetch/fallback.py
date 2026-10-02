"""Last-resort egress fallbacks for the fetch layer.

When the static hop fails on transport (connect/timeout - the host is unreachable
or blocked from this box, e.g. Cloudflare refusing our IP), hosted competitors win
the URL via server-side egress. We can't change our egress, but we can recover the
content from a public mirror: the Wayback Machine.

SSRF: archive.org and the snapshot host are public; both still flow through
fetch_static's per-hop guard, so a snapshot URL that resolves to a private/metadata
IP is rejected just like any other URL.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

from ..models import record_stage
from .static import FetchError, fetch_static

_AVAILABILITY_API = "https://archive.org/wayback/available?url="
ARCHIVE_TIMEOUT = 10  # per request; the step is a last resort, not worth a 30 s wait
ARCHIVE_COOLDOWN = 30 * 60
# Production 2026-10-02: archive.org answered 429 on every endpoint from the VPS IP, so
# every exhausted ladder paid two doomed requests. A status block (or this many transport
# failures in a row) turns the step off for ARCHIVE_COOLDOWN.
_MAX_TRANSPORT_FAILS = 3
_off_until = 0.0
_transport_fails = 0

# `id_` after the timestamp asks Wayback for the original bytes, without its toolbar/rewrite.
_SNAPSHOT_TS = re.compile(r"(/web/\d+)/")


async def fetch_via_archive(url: str, *, client, timeout: float = 30) -> dict | None:
    """Fetch the latest Wayback Machine snapshot of ``url``.

    Queries the availability API, reads ``archived_snapshots.closest.url``, then
    ``fetch_static`` that snapshot (raw ``id_`` form). Returns ``{final_url, status, html,
    render_path: 'archive'}`` or ``None`` if there is no snapshot, the step is cooling
    down, or anything fails; each outcome records a ``fetch.archive_*`` stage.

    Never raises - any problem (transport, SSRF on the lookup/snapshot, bad JSON,
    no snapshot) collapses to ``None`` so the caller can fall through to its
    original error.
    """
    global _off_until, _transport_fails
    if time.monotonic() < _off_until:
        record_stage("fetch.archive_skip_cooldown")
        return None
    timeout = min(timeout, ARCHIVE_TIMEOUT)
    try:
        # Percent-encode the target URL into the query string so its own `&`/`?`/`#`
        # cannot inject extra query params into the availability request.
        avail = await fetch_static(
            _AVAILABILITY_API + quote(url, safe=""), client=client, timeout=timeout
        )
        snapshot = json.loads(avail["html"]).get("archived_snapshots", {}).get("closest")
        if not snapshot or not snapshot.get("url"):
            _transport_fails = 0  # archive.org answered; there is just no copy
            record_stage("fetch.archive_fail_none")
            return None
        raw = _SNAPSHOT_TS.sub(r"\1id_/", snapshot["url"], count=1)
        snap = await fetch_static(raw, client=client, timeout=timeout)
    except FetchError as exc:
        if exc.code == "blocked_by_antibot":
            _off_until = time.monotonic() + ARCHIVE_COOLDOWN
            record_stage(f"fetch.archive_fail_{exc.status}")
        else:
            _transport_fails += 1
            if _transport_fails >= _MAX_TRANSPORT_FAILS:
                _off_until = time.monotonic() + ARCHIVE_COOLDOWN
                _transport_fails = 0
            record_stage("fetch.archive_fail_transport")
        return None
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        record_stage("fetch.archive_fail_other")  # bad JSON, SSRF on the snapshot host
        return None

    _transport_fails = 0
    return {
        "final_url": snap["final_url"],
        "status": snap["status"],
        "html": snap["html"],
        "render_path": "archive",
    }
