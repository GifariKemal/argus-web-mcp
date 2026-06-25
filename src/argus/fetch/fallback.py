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
from urllib.parse import quote

from .static import fetch_static

_AVAILABILITY_API = "https://archive.org/wayback/available?url="


async def fetch_via_archive(url: str, *, client, timeout: float = 30) -> dict | None:
    """Fetch the latest Wayback Machine snapshot of ``url``.

    Queries the availability API, reads ``archived_snapshots.closest.url``, then
    ``fetch_static`` that snapshot. Returns ``{final_url, status, html,
    render_path: 'archive'}`` or ``None`` if there is no snapshot or anything fails.

    Never raises - any problem (transport, SSRF on the lookup/snapshot, bad JSON,
    no snapshot) collapses to ``None`` so the caller can fall through to its
    original error.
    """
    try:
        # Percent-encode the target URL into the query string so its own `&`/`?`/`#`
        # cannot inject extra query params into the availability request.
        avail = await fetch_static(
            _AVAILABILITY_API + quote(url, safe=""), client=client, timeout=timeout
        )
        data = json.loads(avail["html"])
        snapshot = data["archived_snapshots"]["closest"]["url"]
        snap = await fetch_static(snapshot, client=client, timeout=timeout)
    except Exception:  # noqa: BLE001 - any failure must degrade to None, never raise
        return None

    return {
        "final_url": snap["final_url"],
        "status": snap["status"],
        "html": snap["html"],
        "render_path": "archive",
    }
