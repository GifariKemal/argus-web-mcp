"""Tests for the per-host politeness/throttle + circuit-breaker (offline, injected clock).

A fake monotonic clock and a recording fake async-sleep make every test deterministic and
instant: no real wall-clock time passes. `sleep` records the durations it was asked to wait
and advances the fake clock by that amount, mimicking a real sleep without blocking.
"""

import pytest

from argus.fetch.throttle import CircuitOpen, HostThrottle


class FakeClock:
    """Manually-advanced monotonic clock. `now()` is the injectable `clock` callable."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeSleep:
    """Recording async sleep: stores each requested duration and advances the clock."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleep(clock: FakeClock) -> FakeSleep:
    return FakeSleep(clock)


@pytest.fixture
def throttle(clock: FakeClock, sleep: FakeSleep) -> HostThrottle:
    return HostThrottle(
        min_interval=1.0,
        failure_threshold=5,
        cooldown=60.0,
        clock=clock.now,
        sleep=sleep,
    )


@pytest.mark.asyncio
async def test_first_acquire_no_wait(throttle: HostThrottle, sleep: FakeSleep):
    await throttle.acquire("example.com")
    assert sleep.calls == []


@pytest.mark.asyncio
async def test_courtesy_delay_same_host(throttle: HostThrottle, clock: FakeClock, sleep: FakeSleep):
    await throttle.acquire("example.com")
    clock.advance(0.3)  # only 0.3s elapsed, min_interval is 1.0
    await throttle.acquire("example.com")
    assert sleep.calls == pytest.approx([0.7])  # waits the remaining 0.7s


@pytest.mark.asyncio
async def test_different_hosts_no_wait(throttle: HostThrottle, sleep: FakeSleep):
    await throttle.acquire("a.com")
    await throttle.acquire("b.com")  # different host: no courtesy delay
    assert sleep.calls == []


@pytest.mark.asyncio
async def test_no_wait_when_interval_elapsed(
    throttle: HostThrottle, clock: FakeClock, sleep: FakeSleep
):
    await throttle.acquire("example.com")
    clock.advance(1.5)  # more than min_interval
    await throttle.acquire("example.com")
    assert sleep.calls == []


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold(throttle: HostThrottle, sleep: FakeSleep):
    for _ in range(5):  # failure_threshold
        throttle.record_failure("bad.com")
    with pytest.raises(CircuitOpen):
        await throttle.acquire("bad.com")
    assert sleep.calls == []  # raised without sleeping


@pytest.mark.asyncio
async def test_breaker_stays_closed_below_threshold(throttle: HostThrottle):
    for _ in range(4):  # one short of threshold
        throttle.record_failure("ok.com")
    await throttle.acquire("ok.com")  # must not raise


@pytest.mark.asyncio
async def test_success_resets_failure_count(throttle: HostThrottle):
    for _ in range(4):
        throttle.record_failure("flap.com")
    throttle.record_success("flap.com")  # reset mid-way
    for _ in range(4):
        throttle.record_failure("flap.com")
    await throttle.acquire("flap.com")  # 4 < threshold after reset -> still closed


@pytest.mark.asyncio
async def test_breaker_half_opens_after_cooldown(throttle: HostThrottle, clock: FakeClock):
    for _ in range(5):
        throttle.record_failure("down.com")
    clock.advance(61.0)  # past cooldown of 60.0
    await throttle.acquire("down.com")  # half-open: one trial allowed


@pytest.mark.asyncio
async def test_open_breaker_within_cooldown_still_blocks(throttle: HostThrottle, clock: FakeClock):
    for _ in range(5):
        throttle.record_failure("down.com")
    clock.advance(30.0)  # still within cooldown
    with pytest.raises(CircuitOpen):
        await throttle.acquire("down.com")


@pytest.mark.asyncio
async def test_half_open_success_closes_breaker(throttle: HostThrottle, clock: FakeClock):
    for _ in range(5):
        throttle.record_failure("down.com")
    clock.advance(61.0)
    await throttle.acquire("down.com")  # half-open trial
    throttle.record_success("down.com")  # trial succeeded -> close + reset
    # Breaker is now closed: failures restart from zero, so 4 more must stay closed.
    for _ in range(4):
        throttle.record_failure("down.com")
    await throttle.acquire("down.com")


@pytest.mark.asyncio
async def test_half_open_failure_reopens(throttle: HostThrottle, clock: FakeClock):
    for _ in range(5):
        throttle.record_failure("down.com")
    clock.advance(61.0)
    await throttle.acquire("down.com")  # half-open trial allowed
    throttle.record_failure("down.com")  # trial failed -> breaker open again
    with pytest.raises(CircuitOpen):
        await throttle.acquire("down.com")


@pytest.mark.asyncio
async def test_concurrent_same_host_acquires_are_spaced(throttle: HostThrottle, sleep: FakeSleep):
    """N concurrent acquirers must self-queue at min_interval spacing, not burst together.

    Before the slot-reservation fix, later acquirers read the same stale last_request
    (written only AFTER the sleep), computed wait<=0, and fired together: only ONE sleep
    recorded and the final reserved slot advanced a single interval. With reserved slots,
    every follower sleeps and the slots are exactly min_interval apart.
    """
    import asyncio

    await asyncio.gather(
        throttle.acquire("example.com"),
        throttle.acquire("example.com"),
        throttle.acquire("example.com"),
    )
    # follower coroutines each waited one interval (FakeSleep advances the shared clock,
    # so each recorded duration is 1.0); the final slot is start + 2 * min_interval.
    assert sleep.calls == [1.0, 1.0]
    assert throttle._hosts["example.com"].last_request == 1002.0


@pytest.mark.asyncio
async def test_concurrent_different_hosts_do_not_wait(throttle: HostThrottle, sleep: FakeSleep):
    import asyncio

    await asyncio.gather(throttle.acquire("a.com"), throttle.acquire("b.com"))
    assert sleep.calls == []
