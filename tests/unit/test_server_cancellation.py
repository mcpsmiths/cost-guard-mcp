"""Regression test for the run_query_bounded MCP-cancellation fix.

Verified directly against the installed mcp SDK in .venv: the SDK's own tool-call
sync-wrapping helper (mcp.server.mcpserver.utilities.func_metadata.FuncMetadata.call_fn)
runs a plain-function tool via anyio.to_thread.run_sync() with no abandon_on_cancel
argument, so anyio's default (abandon_on_cancel=False) applies - it shields the calling
task from a client's `notifications/cancelled` until the worker thread finishes on its
own. server.py's run_query_bounded is now async and calls anyio.to_thread.run_sync()
itself with abandon_on_cancel=True, so a client cancellation detaches promptly instead.

This test spawns the real server as a subprocess over real MCP stdio transport (mirroring
the smoke test in .github/workflows/tests.yml), with the BigQuery client library mocked
inside that subprocess to simulate a slow warehouse call - never a real network call, and
no live credentials are used or required. It then cancels the client-side asyncio Task
wrapping the call_tool(...) coroutine, which makes the mcp SDK send a real
`notifications/cancelled` for that request's id (see mcp.shared.jsonrpc_dispatcher's
caller-cancellation handling), and asserts the call returns promptly rather than blocking
for the mocked call's full artificial duration.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

# How long the subprocess's mocked warehouse call artificially blocks for. Large enough
# that a regression (cancellation not honored) would make this test fail slowly and
# obviously rather than passing by accident on a slow CI box.
_SLOW_WAREHOUSE_CALL_SECONDS = 8

# The subprocess entry point: patches the BigQuery client library with a mock that blocks
# for _SLOW_WAREHOUSE_CALL_SECONDS before returning, then runs the real stdio server. Written
# to a temp file per-test rather than shipped as a repo file, since it exists only to be
# spawned as `python <path>` by stdio_client.
_SLOW_SERVER_SCRIPT = f"""
import time
from unittest.mock import MagicMock

from cost_guard_mcp.engines import bigquery as bigquery_engine


def _slow_result(*args, **kwargs):
    time.sleep({_SLOW_WAREHOUSE_CALL_SECONDS})
    return iter([{{"a": 1}}])


_mock_query_job = MagicMock()
_mock_query_job.total_bytes_processed = 100
_mock_query_job._properties = {{
    "statistics": {{"query": {{"totalBytesProcessedAccuracy": "PRECISE"}}}}
}}
_mock_query_job.result.side_effect = _slow_result

_mock_client = MagicMock()
_mock_client.project = "test-project"
_mock_client.query.return_value = _mock_query_job

# Patch the attributes on the already-imported google.cloud.bigquery module object (shared
# by every module that did `from google.cloud import bigquery`) rather than reassigning
# cost_guard_mcp.engines.bigquery's own import - this must take effect regardless of import
# order relative to cost_guard_mcp.server below.
bigquery_engine.bigquery.Client = MagicMock(return_value=_mock_client)
bigquery_engine.bigquery.QueryJobConfig = MagicMock(return_value=MagicMock())
# Avoid a real BigQuery Reservations API call for the capacity-billing check.
bigquery_engine.is_capacity_billed = lambda project, location="US": False

from cost_guard_mcp.server import main

main()
"""


async def test_client_cancellation_of_run_query_bounded_detaches_promptly(tmp_path: Path) -> None:
    script_path = tmp_path / "slow_bigquery_server.py"
    script_path.write_text(_SLOW_SERVER_SCRIPT)

    params = StdioServerParameters(command=sys.executable, args=[str(script_path)])

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        call_task = asyncio.create_task(
            session.call_tool("run_query_bounded", {"engine": "bigquery", "sql": "SELECT 1"})
        )

        # Give the request time to reach the server and enter the mocked slow warehouse
        # call before we cancel it - a real client cancelling a real in-flight call, not a
        # cancel racing the request's own delivery.
        await asyncio.sleep(1.0)

        started_at = time.monotonic()
        call_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await call_task

        elapsed_seconds = time.monotonic() - started_at

    # A regression (cancellation not honored) would block for close to
    # _SLOW_WAREHOUSE_CALL_SECONDS (8s) before this line is even reached, since the SDK's
    # default sync-wrapping shields the awaited thread call from cancellation until it
    # finishes on its own. A prompt detach returns in a small fraction of that.
    assert elapsed_seconds < _SLOW_WAREHOUSE_CALL_SECONDS / 2
