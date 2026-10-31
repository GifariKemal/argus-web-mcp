"""``extract_article`` off the event loop AND off the GIL.

trafilatura's pruning is pure Python, so threads never ran pages in parallel: 10 pages
took 10.5 s sequentially, 10.7 s on 2 threads and 14.0 s on 4 (GIL thrash), against
6.3 s on 2 processes (measured 2026-10-15). ``research``/``batch_read`` fan out 5-8
extractions at once, so on the 2-vCPU VPS they were slower than serial and starved the
loop. Workers are spawned (never forked: the parent runs ONNX and Chromium threads) and
import only the extract package. ``ARGUS_EXTRACT_WORKERS=0`` keeps the thread path
(tests, tiny hosts). Spawn re-imports the main module in each worker, so a script that
calls into Argus needs an ``if __name__ == "__main__"`` guard (uvicorn has one) or that
setting; without either, every extraction fails as ``extract_failed``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import functools
import logging
import multiprocessing
import os
import time

from ..config import PDF_EXECUTOR, _int
from .article import extract_article

logger = logging.getLogger(__name__)
_pool: cf.ProcessPoolExecutor | None = None
# Typical pages take 0.35-1.5 s. A worker stuck past this is killed: cancelling the await
# never stops a process, and two wedged workers would otherwise queue every later read.
_EXTRACT_TIMEOUT_S = 30.0


def workers() -> int:
    return _int("ARGUS_EXTRACT_WORKERS", min(2, os.cpu_count() or 1))


def _get_pool() -> cf.ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = cf.ProcessPoolExecutor(
            workers(), mp_context=multiprocessing.get_context("spawn")
        )
    return _pool


async def extract_async(html: str, url: str, **kw):
    """``extract_article`` on a worker process (or a thread when workers() == 0)."""
    if workers() <= 0:
        return await asyncio.to_thread(extract_article, html, url, **kw)
    fn = functools.partial(extract_article, html, url, **kw)
    loop = asyncio.get_running_loop()
    pool = _get_pool()
    try:
        return await asyncio.wait_for(loop.run_in_executor(pool, fn), _EXTRACT_TIMEOUT_S)
    except TimeoutError:
        logger.warning("extraction of %s exceeded %gs; recycling workers", url, _EXTRACT_TIMEOUT_S)
        _retire(pool, kill=True)
        return extract_article("", url, **kw)  # same shape, empty content -> empty_content
    except cf.process.BrokenProcessPool:  # a worker died (OOM kill): rebuild once, retry
        _retire(pool)
        pool = _get_pool()
        try:
            return await loop.run_in_executor(pool, fn)
        except (cf.process.BrokenProcessPool, OSError):  # cannot spawn here: serve anyway
            logger.warning("extraction pool unavailable; extracting on a thread")
            _retire(pool)
            return await asyncio.to_thread(extract_article, html, url, **kw)


def _retire(pool: cf.ProcessPoolExecutor, kill: bool = False) -> None:
    """Drop ``pool`` only if it is still the current one: every caller in flight sees the
    same broken pool, and the second must reuse the first's replacement, not kill it.
    ``kill`` ends its workers too (a wedged one ignores shutdown); callers still waiting
    on it get BrokenProcessPool and take the rebuild path."""
    global _pool
    if _pool is pool:
        if kill:
            _kill(pool)
        _pool = None
        pool.shutdown(wait=False, cancel_futures=True)


def _kill(pool: cf.ProcessPoolExecutor) -> None:
    for proc in list((pool._processes or {}).values()):  # no public API before 3.14
        proc.kill()


# --- documents: their own pool ------------------------------------------------------------
# ``read``'s documents (PDF, Office, EPUB, feeds) run here, not on the article pool: a
# hostile file that overruns gets its workers killed, and research/batch_read extractions
# in flight on the article pool must not die with it. The gate admits one job per worker,
# so the timeout counts the job's own run, not time queued behind another document. A slot
# is held until the worker is really done, not until the caller stops waiting.
_doc_pool: cf.ProcessPoolExecutor | None = None
_doc_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_DOC_TIMEOUT_S = 30.0
# The watchdog fires this long after the caller's own timeout, so a caller still waiting
# always sees TimeoutError (and is not retried) before the watchdog kills its pool.
_WATCHDOG_GRACE_S = 1.0


def _doc_workers() -> int:
    return min(2, workers())


def _get_doc_pool() -> cf.ProcessPoolExecutor:
    global _doc_pool
    if _doc_pool is None:
        _doc_pool = cf.ProcessPoolExecutor(
            _doc_workers(), mp_context=multiprocessing.get_context("spawn")
        )
    return _doc_pool


def _gate() -> asyncio.Semaphore:
    global _doc_gate
    loop = asyncio.get_running_loop()
    if _doc_gate is None or _doc_gate[0] is not loop:  # a semaphore belongs to one loop
        _doc_gate = (loop, asyncio.Semaphore(_doc_workers()))
    return _doc_gate[1]


async def run_document(fn, *args, timeout: float | None = None):
    """``fn(*args)`` on the document pool within ``timeout`` (default ``_DOC_TIMEOUT_S``).
    A job that times out or kills its worker takes the pool down and gets the error, never a
    retry: the file that killed one worker would kill the next. A job whose pool another job
    took down is retried once on a fresh pool. Thread mode (workers() == 0) runs on the PDF
    thread, since PyMuPDF is not thread-safe."""
    call = functools.partial(fn, *args)
    timeout = timeout or _DOC_TIMEOUT_S
    loop = asyncio.get_running_loop()
    if workers() <= 0:
        timed = functools.partial(_timed, call, time.monotonic() + timeout)
        return await asyncio.wait_for(loop.run_in_executor(PDF_EXECUTOR, timed), timeout)
    for attempt in (1, 2):
        pool, fut = await _submit_doc(call, timeout)
        try:
            # shield: a cancelled caller leaves the job running, still holding its slot
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except TimeoutError:
            _retire_doc(pool)
            raise
        except cf.process.BrokenProcessPool:
            if _doc_pool is pool or attempt == 2:  # this job broke it (or broke it twice)
                _retire_doc(pool)
                raise
            logger.info("document pool killed under a job by another one; retrying once")


async def _submit_doc(call, timeout: float) -> tuple[cf.ProcessPoolExecutor, asyncio.Future]:
    """Take a gate slot and submit ``call``. The slot comes back when the worker finishes
    (or its pool dies), and a watchdog kills the pool if the job outlives ``timeout``, both
    whether or not anyone still awaits the result (a disconnected client cancels its read,
    not the worker)."""
    loop = asyncio.get_running_loop()
    gate = _gate()
    await gate.acquire()
    call = functools.partial(_timed, call, time.monotonic() + timeout)  # job_deadline()
    try:
        pool = _get_doc_pool()
        try:
            fut = loop.run_in_executor(pool, call)
        except cf.process.BrokenProcessPool:  # died under another job, not retired yet
            _retire_doc(pool)
            pool = _get_doc_pool()
            fut = loop.run_in_executor(pool, call)
    except BaseException:  # nothing was submitted: nothing holds the slot
        gate.release()
        raise
    watchdog = loop.call_later(timeout + _WATCHDOG_GRACE_S,
                               lambda: fut.done() or _retire_doc(pool))

    def _done(f: asyncio.Future) -> None:
        gate.release()
        watchdog.cancel()
        if not f.cancelled():
            f.exception()  # retrieved: an orphan's error is not "never retrieved" noise

    fut.add_done_callback(_done)
    return pool, fut


# The running job's kill time, as seen inside its worker. A job that starts a subprocess
# (tesseract) bounds it by this, so the child ends before the pool kills the worker.
_job_deadline: float | None = None


def job_deadline() -> float | None:
    """When the pool kills the running document job (``time.monotonic()``), or None outside
    one. The monotonic clock is system-wide, so the parent's value holds in the worker."""
    return _job_deadline


def _timed(call, deadline: float):
    global _job_deadline
    _job_deadline = deadline
    try:
        return call()
    finally:
        _job_deadline = None


def _retire_doc(pool: cf.ProcessPoolExecutor) -> None:
    global _doc_pool
    if _doc_pool is pool:
        _kill(pool)
        _doc_pool = None
        pool.shutdown(wait=False, cancel_futures=True)


def warm() -> None:
    """Spawn the workers now so the first read does not pay their import (~1 s each)."""
    if workers() > 0:
        for _ in range(workers()):
            fut = _get_pool().submit(extract_article, "<p>warm</p>", "https://localhost/warm")
            fut.add_done_callback(_log_warm)


def _log_warm(fut: cf.Future) -> None:
    if (exc := fut.exception()) is not None:  # the first read would hit the same error
        logger.warning("extraction worker warm-up failed: %r", exc)


def shutdown() -> None:
    """Stop both pools (lifespan exit, tests)."""
    global _pool, _doc_pool
    for pool in (_pool, _doc_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    _pool = _doc_pool = None
