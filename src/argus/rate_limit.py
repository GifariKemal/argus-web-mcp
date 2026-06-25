"""In-memory sliding-window rate limiter per client identity.

Pure stdlib; no external deps. Thread-safe via GIL (single-process).
For multi-process deploy, promote to Redis-backed limiter (out of scope for P1).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class _Window:
    """Fixed-size sliding window of request timestamps for one identity."""

    requests: deque[float] = field(default_factory=lambda: deque())


class RateLimiter:
    """Sliding-window rate limiter."""

    def __init__(
        self,
        *,
        rpm: int = 60,
        burst: int = 10,
        window_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ):
        self._rpm = rpm
        self._burst = burst
        self._window = window_seconds
        self._clock = clock or time.monotonic
        self._clients: dict[str, _Window] = {}

    def _prune(self, w: _Window, now: float) -> None:
        cutoff = now - self._window
        while w.requests and w.requests[0] <= cutoff:
            w.requests.popleft()

    def check(self, identity: str) -> tuple[bool, dict]:
        """Return (allowed, info_dict)."""
        now = self._clock()
        w = self._clients.setdefault(identity, _Window())
        self._prune(w, now)

        # Burst check (hard ceiling)
        if len(w.requests) >= self._burst:
            retry_after = int(w.requests[0] + self._window - now) + 1
            return False, {
                "identity": identity,
                "allowed": False,
                "reason": "burst_limit",
                "current": len(w.requests),
                "limit": self._burst,
                "retry_after_seconds": max(1, retry_after),
            }

        # RPM check
        if len(w.requests) >= self._rpm:
            retry_after = int(w.requests[0] + self._window - now) + 1
            return False, {
                "identity": identity,
                "allowed": False,
                "reason": "rate_limit",
                "current": len(w.requests),
                "limit": self._rpm,
                "retry_after_seconds": max(1, retry_after),
            }

        w.requests.append(now)
        return True, {
            "identity": identity,
            "allowed": True,
            "current": len(w.requests),
            "limit": self._rpm,
        }

    def status(self, identity: str) -> dict:
        now = self._clock()
        w = self._clients.get(identity, _Window())
        self._prune(w, now)
        return {
            "identity": identity,
            "current": len(w.requests),
            "limit": self._rpm,
            "burst_limit": self._burst,
            "window_seconds": self._window,
        }
