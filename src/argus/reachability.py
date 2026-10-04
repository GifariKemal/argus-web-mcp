"""Daily reachability map: which sites answer this box, directly and through the egress proxy.

Which sites block the VPS IP (and which ones also block WARP) drifts over time, and it
decides two things: what belongs in ARGUS_EGRESS_PROXY_HOSTS, and which official-API
adapter (fetch/adapters.py) is worth writing next. This measures it instead of guessing:
one GET per site per path, once a day, with Argus's own user agent and SSRF-safe clients,
redirects not followed (a 3xx counts as reachable).

A listed host that fails THROUGH the proxy is the failure a WARP health probe cannot see
(WARP fine, the site now blocks WARP too); server.health exposes it so uptime.yml alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from . import config
from .fetch.render import _looks_blocked
from .fetch.static import _DEFAULT_UA

# Sites measured on 2026-10-04 (CHANGELOG 0.4.23) plus a control. Path = a page Argus
# users actually read there, not just the root.
SITES: dict[str, str] = {
    "web.archive.org": "https://web.archive.org/web/2024/https://example.com/",
    "reuters.com": "https://www.reuters.com/world/",
    "wsj.com": "https://www.wsj.com/",
    "fxstreet.com": "https://www.fxstreet.com/news",
    "reddit.com": "https://www.reddit.com/r/esp32/.json",
    "medium.com": "https://medium.com/",
    "quora.com": "https://www.quora.com/",
    "bloomberg.com": "https://www.bloomberg.com/markets",
    "investing.com": "https://www.investing.com/news/",
    "forexfactory.com": "https://www.forexfactory.com/calendar",
    "npmjs.com": "https://www.npmjs.com/package/react",
    "stackoverflow.com": "https://stackoverflow.com/questions/11227809",
    "example.com": "https://example.com/",  # control: if this fails, the box is offline
}


_MAX_BODY = 256 * 1024


async def _probe(client, url: str) -> int | str:
    """HTTP status; "challenge" for a 2xx/3xx that is an anti-bot page (Bloomberg-style 200
    walls); the exception name when no answer came back. Tried twice."""
    out: int | str = "none"
    for _ in range(2):
        try:
            async with client.stream("GET", url, headers={"user-agent": _DEFAULT_UA},
                                     timeout=20) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():  # enough to spot a wall, never more
                    body += chunk
                    if len(body) >= _MAX_BODY:
                        break
                status = r.status_code
                text = body.decode(r.encoding or "utf-8", "replace")
            out = "challenge" if status < 400 and _looks_blocked(text, status) else status
        except Exception as exc:  # noqa: BLE001 - the measurement records any failure
            out = type(exc).__name__
        if ok(out):
            break
    return out


def ok(result: int | str) -> bool:
    return isinstance(result, int) and result < 400


async def measure(direct, proxied=None) -> dict:
    """``{"at": iso, "sites": {site: {"direct": result, "warp": result}}}``; ``warp`` only
    when a proxied client is given."""
    sites = {}
    for site, url in SITES.items():  # ponytail: sequential, ~26 requests once a day
        row = {"direct": await _probe(direct, url)}
        if proxied is not None:
            row["warp"] = await _probe(proxied, url)
        sites[site] = row
    return {"at": datetime.now(UTC).isoformat(timespec="seconds"), "sites": sites}


def confirmed_failing(previous: dict | None, current: dict | None) -> list[str]:
    """Listed hosts failing through the proxy in two runs in a row: one bad minute must
    not turn the uptime alert red for a day."""
    return sorted(set(failing_listed(previous)) & set(failing_listed(current)))


def failing_listed(report: dict | None) -> list[str]:
    """Hosts on the egress list that did not answer through the proxy in ``report``."""
    if not report:
        return []
    return sorted(site for site, row in report["sites"].items()
                  if config.egress_proxy_for(site) and "warp" in row and not ok(row["warp"]))
