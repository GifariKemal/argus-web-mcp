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

import httpx

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
    "api.fxtwitter.com": "https://api.fxtwitter.com/status/20",  # x_search / x.com read
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


# Search engines are measured too: a free engine that starts blocking the VPS IP fails
# silently (SearXNG answers from the others), and only a per-engine probe notices.
ENGINE_QUERY = "python asyncio tutorial"


async def measure_engines(base_url: str, engines: list[str]) -> dict[str, int]:
    """``{engine: results attributed to it}`` for one fixed query each, straight at the
    SearXNG backend (trusted, compose network). 0 = answered nothing or errored."""
    counts: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=20) as client:
        for engine in engines:
            try:
                r = await client.get(f"{base_url}/search", params={
                    "q": ENGINE_QUERY, "format": "json", "engines": engine})
                counts[engine] = sum(1 for x in r.json().get("results", [])
                                     if x.get("engine") == engine)
            except Exception:  # noqa: BLE001 - a failed probe is a dead engine
                counts[engine] = 0
    return counts


def confirmed_dead_engines(previous: dict | None, current: dict | None) -> list[str]:
    """Engines that answered nothing in two runs in a row (same two-strike rule as hosts)."""
    if not previous or not current:
        return []
    return sorted(e for e, n in current.items() if n == 0 and previous.get(e) == 0)


# One known-good input per API adapter (the 2026-11-23 Agent-Reach audit's live_test idea).
# A broken adapter returns None and the read silently falls back to the page, so only a
# canary notices: Reddit's thread feeds 404ing and X's new web build both went unseen for
# days in 2026-11. X GraphQL is left out on purpose: its state is already `x_account` in
# /health, and every call spends the account's budget.
CANARIES: dict[str, str] = {
    "youtube": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    "hn": "https://news.ycombinator.com/item?id=8863",
    "telegram": "https://t.me/s/telegram",
    "stackexchange": "https://stackoverflow.com/questions/11227809",
    "npm": "https://www.npmjs.com/package/express",
    "polymarket": "https://polymarket.com/event/presidential-election-winner-2024",
    "github": "https://github.com/astral-sh/uv/issues/1",
    "fxtwitter": "https://x.com/jack/status/20",
}


def _adapter(name: str):
    """The adapter function behind a canary, looked up at call time (patchable)."""
    if name == "fxtwitter":
        from .x import fxtwitter
        return fxtwitter.fetch_x
    from .fetch import adapters
    return getattr(adapters, f"fetch_{name}")


async def measure_adapters(client, only: list[str] | None = None) -> dict[str, bool]:
    """``{canary: adapter answered}`` through the real adapters and the SSRF-safe client;
    ``only`` re-measures just those canaries."""
    out: dict[str, bool] = {}
    for name, url in CANARIES.items():  # ponytail: sequential, 8 requests once a day
        if only is not None and name not in only:
            continue
        try:
            out[name] = await _adapter(name)(url, client=client, timeout=20) is not None
        except Exception:  # noqa: BLE001 - adapters never raise, but a canary must not either
            out[name] = False
    return out


def confirmed_failing_adapters(previous: dict | None, current: dict | None) -> list[str]:
    """Adapters whose canary failed in two runs in a row (same two-strike rule)."""
    if not previous or not current:
        return []
    return sorted(n for n, good in current.items() if not good and previous.get(n) is False)
