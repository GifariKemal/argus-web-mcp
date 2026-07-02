"""Watch subsystem: register a URL to poll, diff its content, notify a webhook.

A watch fetches a URL on an interval, computes a stable content signature (whole
content, or the string value of a CSS/XPath ``selector``), and when the signature
changes from the previous check it POSTs a JSON payload to a user-supplied webhook
(e.g. ForexFactory calendar / CFTC COT -> Telegram bridge).

Design notes
------------
* **Offline-injectable.** The orchestrator wires the real poller; everything here
  takes an injected async ``fetch_fn``, an injected httpx-like ``client``, and an
  explicit ``now`` float - no real network, sleep, or wall clock - so it is fully
  unit-testable and deterministic.
* **Persistence.** Watches live in a JSON file (list of watch dicts) so they
  survive a process restart. Every mutation rewrites the file atomically (temp +
  ``os.replace``).
* **Baseline-vs-change.** The *first* check of a watch only establishes a baseline
  hash (``changed=False``, no notification). A change is reported only when a prior
  ``last_hash`` exists and the new hash differs.
* **Webhook is a trust boundary.** The webhook is a user-supplied URL, so it is
  SSRF-validated (scheme allowlist + resolve-and-validate against private/metadata
  ranges) *before* any POST. A blocked webhook is never contacted.
* **Resilient.** A single watch's fetch/extract/delivery failure never aborts the
  rest of the poll; ``check_watch`` and ``deliver`` never raise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Protocol

from parsel import Selector

from .security.ssrf import SSRFError, aresolve_and_validate, validate_url

logger = logging.getLogger("argus.watch")

# fetch result keys that may hold the page content, most-specific first.
_CONTENT_KEYS = ("content", "markdown", "html", "text")

FetchFn = Callable[[str], Awaitable[dict]]


class _HttpClient(Protocol):  # pragma: no cover - typing only
    async def post(self, url: str, *, json: Any = ...) -> Any: ...


@dataclass
class Watch:
    """A single registered watch. ``id`` is a short stable hash of its identity."""

    id: str
    url: str
    selector: str | None
    interval_s: int
    webhook: str
    last_hash: str | None = None
    last_check: float | None = None


def _watch_id(url: str, selector: str | None, webhook: str) -> str:
    raw = f"{url}\x00{selector or ''}\x00{webhook}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def content_signature(text: str, selector_value: str | None = None) -> str:
    """Stable sha256 hex of ``selector_value`` if given, else the full ``text``."""
    basis = selector_value if selector_value is not None else text
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


def _select_value(content: str, selector: str) -> str | None:
    """Return the string value of the first ``selector`` match (CSS or XPath)."""
    sel = Selector(text=content)
    nodes = sel.xpath(selector) if selector.lstrip().startswith(("//", "(")) else sel.css(selector)
    if not nodes:
        return None
    node = nodes[0]
    val = node.get() if isinstance(node.root, str) else node.xpath("string(.)").get()
    if val is None:
        return None
    val = val.strip()
    return val or None


def _content_of(fetched: dict) -> str:
    for key in _CONTENT_KEYS:
        val = fetched.get(key)
        if isinstance(val, str):
            return val
    return ""


class WatchStore:
    """JSON-file-backed collection of watches; persists on every mutation."""

    def __init__(self, path: str = "~/.argus/watches.json") -> None:
        self._path = Path(os.path.expanduser(path))
        self._watches: list[Watch] = self._load()

    def _load(self) -> list[Watch]:
        if not self._path.exists():
            return []
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("watch store unreadable, starting empty: %s", self._path)
            return []
        allowed = {f.name for f in fields(Watch)}
        return [Watch(**{k: v for k, v in row.items() if k in allowed}) for row in rows]

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps([asdict(w) for w in self._watches], indent=2)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self._path)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _find(self, watch_id: str) -> Watch | None:
        return next((w for w in self._watches if w.id == watch_id), None)

    def add(self, url: str, selector: str | None, interval_s: int, webhook: str) -> Watch:
        """Register a watch (idempotent: dedups by id). Persists; returns the watch."""
        wid = _watch_id(url, selector, webhook)
        existing = self._find(wid)
        if existing is not None:
            return existing
        watch = Watch(id=wid, url=url, selector=selector, interval_s=interval_s, webhook=webhook)
        self._watches.append(watch)
        self._persist()
        return watch

    def list(self) -> list[Watch]:
        return list(self._watches)

    def remove(self, watch_id: str) -> bool:
        """Remove by id. Persists. Returns True if a watch was removed."""
        watch = self._find(watch_id)
        if watch is None:
            return False
        self._watches.remove(watch)
        self._persist()
        return True

    def update_state(self, watch_id: str, last_hash: str | None, last_check: float) -> None:
        """Record the latest hash/check time for a watch. Persists. ``last_hash`` may be
        None (an errored check advances last_check while keeping no baseline). Atomic:
        a persist failure rolls the in-memory fields back so memory and disk never
        diverge (a diverged hash would re-deliver an already-sent change after restart).
        """
        watch = self._find(watch_id)
        if watch is None:
            return
        old_hash, old_check = watch.last_hash, watch.last_check
        watch.last_hash = last_hash
        watch.last_check = last_check
        try:
            self._persist()
        except OSError:
            watch.last_hash, watch.last_check = old_hash, old_check
            raise


async def check_watch(w: Watch, *, fetch_fn: FetchFn, now: float) -> dict:
    """Fetch ``w.url`` and diff against ``w.last_hash``.

    Returns ``{changed, new_hash, value}``. ``changed`` is True only when a prior
    hash exists and differs (first-ever check establishes the baseline ->
    ``changed=False``). Never raises on fetch/extract error -> ``{changed: False,
    error: ...}``.
    """
    try:
        fetched = await fetch_fn(w.url)
        content = _content_of(fetched)
        value = _select_value(content, w.selector) if w.selector else None
        new_hash = content_signature(content, value)
    except Exception as exc:  # noqa: BLE001 - watch must never crash the poller
        logger.warning("watch %s check failed: %s", w.id, exc)
        return {"changed": False, "error": str(exc)}

    changed = w.last_hash is not None and new_hash != w.last_hash
    return {"changed": changed, "new_hash": new_hash, "value": value}


async def deliver(webhook: str, payload: dict, *, client: _HttpClient) -> bool:
    """SSRF-guard ``webhook`` then POST ``payload`` as JSON. Never raises.

    Returns True on a 2xx response, False on non-2xx, transport error, or a webhook
    that fails the SSRF trust boundary (in which case nothing is POSTed).
    """
    try:
        validate_url(webhook)
        from urllib.parse import urlsplit

        parts = urlsplit(webhook)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        await aresolve_and_validate(parts.hostname, port)
    except SSRFError as exc:
        logger.warning("webhook blocked by SSRF guard, not delivering: %s (%s)", webhook, exc)
        return False

    try:
        resp = await client.post(webhook, json=payload)
        return 200 <= resp.status_code < 300
    except Exception as exc:  # noqa: BLE001 - delivery failure is non-fatal
        logger.warning("webhook delivery failed: %s (%s)", webhook, exc)
        return False


async def poll_due(
    store: WatchStore, *, fetch_fn: FetchFn, client: _HttpClient, now: float
) -> list[dict]:
    """Check every due watch, notify on change, persist new state.

    A watch is due when it has never been checked (``last_check is None``) or
    ``now - last_check >= interval_s``. For each due watch we ``check_watch``; on a
    detected change we ``deliver`` the webhook; the watch state is updated in all
    cases - baseline, post-change, AND fetch errors (last_check always advances so
    ``interval_s`` is honored even while a source is failing; an errored check keeps
    the previous ``last_hash`` so no change event is lost). A broken webhook does
    not cause perpetual re-alerting. Resilient: one watch's failure (including a
    state-persist OSError) never aborts the others.
    """
    results: list[dict] = []
    for w in store.list():
        if w.last_check is not None and (now - w.last_check) < w.interval_s:
            continue

        res = await check_watch(w, fetch_fn=fetch_fn, now=now)
        delivered = False

        if res.get("changed"):
            payload = {
                "watch_id": w.id,
                "url": w.url,
                "selector": w.selector,
                "value": res.get("value"),
                "new_hash": res["new_hash"],
                "checked_at": now,
            }
            delivered = await deliver(w.webhook, payload, client=client)

        try:
            store.update_state(w.id, res.get("new_hash", w.last_hash), now)
        except OSError as exc:
            # On persist failure last_hash/last_check do not advance, so a changed watch
            # re-delivers next tick until disk recovers - bounded re-alerting beats a
            # silent outage, and the remaining watches still get their turn.
            logger.warning("watch %s state persist failed: %s", w.id, exc)

        entry = {"id": w.id, "changed": bool(res.get("changed")), "delivered": delivered}
        if "error" in res:
            entry["error"] = res["error"]
        results.append(entry)

    return results
