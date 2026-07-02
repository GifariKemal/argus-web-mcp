"""Per-host politeness throttle + circuit-breaker for the Argus fetch layer.

Good-web-citizen behaviour, pure stdlib:

* **Courtesy delay** - at most one request per `min_interval` seconds to the same host
  (`acquire` awaits the injected `sleep` for the remaining time). Different hosts never
  block each other.
* **Circuit breaker** per host, a closed -> open -> half-open -> closed machine:
    - *closed*: requests pass; `record_failure` counts consecutive failures.
    - *open*: reached after `failure_threshold` consecutive failures; `acquire` raises
      `CircuitOpen` (without sleeping) while still within `cooldown` of the open-time.
    - *half-open*: once `cooldown` elapses, one trial request is allowed through. A
      following `record_success` closes the breaker and resets the count; a
      `record_failure` re-opens it (fresh open-time).

The `clock` (now) and `sleep` (async) are injectable so tests run offline and instant.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class CircuitOpen(Exception):
    """Raised when a host's breaker is open (too many recent consecutive failures)."""

    code = "fetch_failed"


@dataclass
class _HostState:
    last_request: float | None = None  # monotonic time of the most recently reserved slot
    failures: int = 0  # consecutive failures
    opened_at: float | None = None  # monotonic time the breaker opened, else None


class HostThrottle:
    def __init__(
        self,
        *,
        min_interval: float = 1.0,
        failure_threshold: int = 5,
        cooldown: float = 60.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        self._min_interval = min_interval
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._hosts: dict[str, _HostState] = {}

    def _state(self, host: str) -> _HostState:
        return self._hosts.setdefault(host, _HostState())

    async def acquire(self, host: str) -> None:
        st = self._state(host)
        now = self._clock()

        if st.opened_at is not None:
            if now - st.opened_at < self._cooldown:
                raise CircuitOpen(f"breaker open for {host}")
            # Cooldown elapsed -> half-open: clear the open flag, allow one trial through.
            st.opened_at = None

        # Reserve the slot BEFORE awaiting: N concurrent same-host acquirers each take the
        # next free slot (read+write with no await between = race-free on the single loop),
        # so they self-queue at exactly min_interval spacing instead of all reading the same
        # stale last_request, sleeping in parallel, and bursting simultaneously.
        slot = now if st.last_request is None else max(now, st.last_request + self._min_interval)
        st.last_request = slot
        if slot > now:
            await self._sleep(slot - now)

    def record_success(self, host: str) -> None:
        st = self._state(host)
        st.failures = 0
        st.opened_at = None

    def record_failure(self, host: str) -> None:
        st = self._state(host)
        st.failures += 1
        if st.failures >= self._failure_threshold:
            st.opened_at = self._clock()
