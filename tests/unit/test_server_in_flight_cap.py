"""Regression tests for the run_query_bounded in-flight cap (audit fingerprint
run-query-bounded-cancel-releases-limiter-token-while-worker-thread-and-connection-remain-pinned).

server.py dispatches run_query_bounded with anyio.to_thread.run_sync(abandon_on_cancel=True):
on an MCP cancel the awaiting task leaves anyio's thread limiter and returns its token while
the worker thread, its per-call warehouse connection and the running query stay pinned for
up to the 120 s watchdog. Cancel-and-reissue therefore grew the number of live warehouse
threads without any in-process bound. The fix is an explicit BoundedSemaphore whose slot is
acquired non-blocking and released INSIDE the worker thread (anyio's WorkerThread.run skips
the function entirely when the future is already cancelled, so an async-side acquire could
leak a slot), so a cancelled call keeps counting until its thread actually exits.

Every test here patches the stub warehouse call; no network, no credentials.
"""

import asyncio
import threading
import time
from unittest.mock import patch

import anyio.from_thread
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cost_guard_mcp import server
from cost_guard_mcp.config import ConfigError
from cost_guard_mcp.errors import SanitizedEngineError, UserVisibleError
from cost_guard_mcp.types import AccuracyTier, BoundedQueryResult, CostEstimate

_ENV_VAR = "COST_GUARD_MAX_IN_FLIGHT_QUERIES"
_ARGS = ("bigquery", "SELECT 1", None, None, None, None, None, None)


def _ok_result() -> BoundedQueryResult:
    return BoundedQueryResult(
        status="ok",
        estimate=CostEstimate(
            engine="bigquery",
            accuracy_tier=AccuracyTier.PRECISE,
            estimated_bytes=1,
            estimated_cost_usd=0.0,
        ),
        rows=[],
        row_count=0,
    )


class _ObservableSemaphore(threading.BoundedSemaphore):
    """BoundedSemaphore that signals each release, so a test can wait for the worker
    thread's `finally` deterministically instead of sleeping."""

    def __init__(self, value: int) -> None:
        super().__init__(value)
        self.released = threading.Event()

    def release(self, n: int = 1) -> None:
        super().release(n)
        self.released.set()


class _BlockingWarehouseStub:
    """Stands in for server._run_query_bounded: blocks in the worker thread until released."""

    def __init__(self, raise_on_release: Exception | None = None) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.before_execute_seen: list[object] = []
        self._raise = raise_on_release

    def __call__(self, *args, before_execute=None, **kwargs):
        self.calls += 1
        self.before_execute_seen.append(before_execute)
        self.started.set()
        assert self.release.wait(5), "test harness never released the stub"
        if self._raise is not None:
            raise self._raise
        return _ok_result()


async def test_cancelled_call_keeps_its_slot_until_its_thread_exits_then_releases_it():
    stub = _BlockingWarehouseStub()
    semaphore = _ObservableSemaphore(1)
    with (
        patch.object(server, "_RUN_QUERY_BOUNDED_IN_FLIGHT", semaphore),
        patch.object(server, "_run_query_bounded", stub),
    ):
        first = asyncio.create_task(server._run_query_bounded_in_thread(*_ARGS))
        assert await asyncio.to_thread(stub.started.wait, 5)

        cancelled_at = time.monotonic()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        # Prompt MCP-level detach (the v0.3.0 behaviour) is unchanged by the cap...
        assert time.monotonic() - cancelled_at < 1.0
        # ...while the abandoned worker thread is still alive and still owns the only slot.
        assert not semaphore.released.is_set()

        with pytest.raises(UserVisibleError, match=_ENV_VAR):
            await server._run_query_bounded_in_thread(*_ARGS)
        assert stub.calls == 1  # the over-cap call never reached the warehouse stub

        stub.release.set()
        assert await asyncio.to_thread(semaphore.released.wait, 5)  # the thread's finally ran

        # The slot is free again: a new call is admitted and returns the stub's result.
        result = await server._run_query_bounded_in_thread(*_ARGS)
        assert result.status == "ok"
        assert stub.calls == 2


async def test_sanitized_engine_error_releases_the_slot():
    stub = _BlockingWarehouseStub(
        raise_on_release=SanitizedEngineError("bigquery client call failed: redacted")
    )
    stub.release.set()  # fail immediately, no blocking needed
    semaphore = _ObservableSemaphore(1)
    with (
        patch.object(server, "_RUN_QUERY_BOUNDED_IN_FLIGHT", semaphore),
        patch.object(server, "_run_query_bounded", stub),
    ):
        with pytest.raises(SanitizedEngineError):
            await server._run_query_bounded_in_thread(*_ARGS)
        assert semaphore.released.is_set()

        # The following call is admitted (it raises the same stub error, not the cap error).
        with pytest.raises(SanitizedEngineError):
            await server._run_query_bounded_in_thread(*_ARGS)
        assert stub.calls == 2


async def test_slot_is_acquired_inside_the_worker_thread_and_hook_is_forwarded():
    stub = _BlockingWarehouseStub()
    stub.release.set()
    semaphore = _ObservableSemaphore(1)
    with (
        patch.object(server, "_RUN_QUERY_BOUNDED_IN_FLIGHT", semaphore),
        patch.object(server, "_run_query_bounded", stub),
        patch("anyio.to_thread.run_sync", wraps=anyio.to_thread.run_sync) as run_sync,
    ):
        await server._run_query_bounded_in_thread(*_ARGS)

    dispatched = run_sync.await_args.args[0]
    # The slot-holding wrapper (not the bare tool body) is what runs in the thread, and the
    # cooperative cancel hook from the previous fix still reaches the tool body through it.
    assert dispatched.func is server._run_query_bounded_holding_in_flight_slot
    assert dispatched.args == _ARGS
    assert dispatched.keywords == {"before_execute": anyio.from_thread.check_cancelled}
    assert run_sync.await_args.kwargs["abandon_on_cancel"] is True
    assert stub.before_execute_seen == [anyio.from_thread.check_cancelled]


async def test_over_cap_refusal_reaches_the_model_as_a_tool_error_naming_the_variable():
    stub = _BlockingWarehouseStub()
    semaphore = _ObservableSemaphore(1)
    with (
        patch.object(server, "_RUN_QUERY_BOUNDED_IN_FLIGHT", semaphore),
        patch.object(server, "_run_query_bounded", stub),
    ):
        first = asyncio.create_task(server._run_query_bounded_in_thread(*_ARGS))
        assert await asyncio.to_thread(stub.started.wait, 5)
        try:
            # Through the real as_tool_error wrapper the tool is registered with: the model
            # sees the hint (ToolError), not an opaque "Error executing tool" (DECISIONS #4).
            with pytest.raises(ToolError, match=_ENV_VAR) as excinfo:
                await server._run_query_bounded_safe(*_ARGS)
            assert "cancelled call keeps its slot" in str(excinfo.value)
            assert stub.calls == 1
        finally:
            stub.release.set()
            await first


def test_default_in_flight_cap_is_eight_and_the_module_semaphore_is_bounded():
    assert server._DEFAULT_MAX_IN_FLIGHT_QUERIES == 8
    assert server._MAX_IN_FLIGHT_QUERIES_ENV_VAR == _ENV_VAR
    assert isinstance(server._RUN_QUERY_BOUNDED_IN_FLIGHT, type(threading.BoundedSemaphore()))


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 8), ("", 8), ("  ", 8), ("3", 3), ("40", 40), (" 8 ", 8)]
)
def test_load_max_in_flight_queries_accepts_unset_and_positive_integers(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(_ENV_VAR, raw)
    assert server._load_max_in_flight_queries() == expected


# Python int() tolerates a sign, a leading zero, "_" separators, hex-looking text and
# non-ASCII digits; a cap is a plain positive decimal integer and nothing else
# (PR #86 runtime verification, container/CLI matrix).
@pytest.mark.parametrize(
    "raw", ["0", "-1", "abc", "3.5", "8 queries", "+3", "08", "1_0", "0x10", "\u0663", "1 2"]
)
def test_load_max_in_flight_queries_rejects_non_positive_or_non_integer_values(monkeypatch, raw):
    monkeypatch.setenv(_ENV_VAR, raw)
    with pytest.raises(ConfigError, match=_ENV_VAR) as excinfo:
        server._load_max_in_flight_queries()
    # Names the variable, never echoes the rejected value (no untrusted text in the error).
    assert raw not in str(excinfo.value)
