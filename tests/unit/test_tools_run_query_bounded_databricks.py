"""Gate-level regression tests for the Databricks leg of run_query_bounded.

These exercise the real engines/databricks.py explain_estimate (only the connector is
mocked) through tools/run_query_bounded.py, so they prove the fail-closed cap gate sees
what the engine really emits - not a hand-built CostEstimate. They live in their own
module rather than test_tools_run_query_bounded.py so that file keeps a single writer.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from databricks.sdk.service.sql import GetWarehouseResponse, State

from cost_guard_mcp.tools.run_query_bounded import run_query_bounded
from cost_guard_mcp.types import RefusalReason

# Audit: databricks-dollar-estimate-floor-when-size-statistics-absent
_PLAN_WITHOUT_STATISTICS = [("== Optimized Logical Plan ==",), ("Relation[a,b] csv",)]
# 50 TB on the default X-Small Serverless warehouse: 6 DBU/h * 0.70 USD * (30 s * 100x) = 3.5 USD.
_PLAN_WITH_50TB_LEAF = [("Relation[a] parquet, Statistics(sizeInBytes=50.0 TB, rowCount=1)",)]
_EXPECTED_50TB_COST_USD = 3.5
# Audit: databricks-explain-cost-size-regex-rejects-iec-unit-suffixes - the docs fixture in
# the Spark 3.x shape (IEC suffix on the leaf, a 20-byte Aggregate node above it).
_IEC_PLAN_TEXT = (
    "== Optimized Logical Plan ==\n"
    "Aggregate [count(1) AS count(1)#2L], Statistics(sizeInBytes=20.0 B, rowCount=1)\n"
    "+- Relation[ss_store_sk,ss_sold_date_sk] parquet, "
    "Statistics(sizeInBytes=134.6 GiB, rowCount=2.88E+9)\n"
)
_PLAN_IEC_SHAPE = [(_IEC_PLAN_TEXT,)]
_IEC_LEAF_BYTES = int(134.6 * 1024**3)
_PLAN_SENTINEL_ONLY = [("Relation[a] parquet, Statistics(sizeInBytes=8.0 EiB, rowCount=1)",)]


# Placeholder value for the mocked config below - never a real credential.
_FAKE_PAT = "fake-pat-placeholder"


@pytest.fixture(autouse=True)
def databricks_warehouse_lookup_unavailable():
    """explain_estimate observes the configured SQL warehouse through the Databricks SDK before
    pricing (the observed-warehouse decision). These gate tests pin the statistics-less -> None
    contract at the caller-asserted X-Small rate, so the lookup is made to fail (no network) and
    the documented fallback keeps that rate - the same path a principal without CAN_VIEW/CAN_USE
    on the warehouse takes."""
    with (
        patch("cost_guard_mcp.engines.databricks.load_databricks_config") as mock_load_config,
        patch("cost_guard_mcp.engines.databricks.Config"),
        patch("cost_guard_mcp.engines.databricks.WorkspaceClient") as mock_workspace_client_cls,
    ):
        mock_load_config.return_value = MagicMock(
            server_hostname="my-workspace.cloud.databricks.com",
            http_path="/sql/1.0/warehouses/abc123",
            access_token=_FAKE_PAT,
            client_id=None,
            client_secret=None,
        )
        mock_workspace_client_cls.return_value.warehouses.get.side_effect = RuntimeError(
            "warehouses.get not stubbed by this test"
        )
        yield SimpleNamespace(workspace_client_cls=mock_workspace_client_cls)


def _mock_explain_connection(rows):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_conn


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine.execute_bounded")
@patch("cost_guard_mcp.engines.databricks._connect")
def test_databricks_dollar_cap_refuses_when_explain_cost_has_no_statistics(
    mock_connect, mock_execute_bounded
):
    # Without plan statistics the engine must not invent a query-independent rate-floor
    # dollar figure: a requested dollar cap is unenforceable, so the gate refuses
    # (DECISIONS.md #5), exactly as the byte cap already does for estimated_bytes=None.
    mock_connect.return_value = _mock_explain_connection(_PLAN_WITHOUT_STATISTICS)

    result = run_query_bounded("databricks", "SELECT * FROM t", max_estimated_cost_usd=100.0)

    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED
    assert result.estimate.estimated_cost_usd is None
    assert result.estimate.estimated_bytes is None
    mock_execute_bounded.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine.execute_bounded")
@patch("cost_guard_mcp.engines.databricks._connect")
def test_databricks_dollar_cap_still_discriminates_when_statistics_present(
    mock_connect, mock_execute_bounded
):
    # Positive control: with statistics the dollar figure is real and the cap discriminates.
    mock_connect.return_value = _mock_explain_connection(_PLAN_WITH_50TB_LEAF)
    mock_execute_bounded.return_value = ([{"a": 1}], 1, False)

    refused = run_query_bounded("databricks", "SELECT * FROM t", max_estimated_cost_usd=0.05)

    assert refused.status == "refused"
    assert refused.reason == RefusalReason.COST_CAP_EXCEEDED
    assert refused.estimate.estimated_cost_usd == _EXPECTED_50TB_COST_USD
    mock_execute_bounded.assert_not_called()

    mock_connect.return_value = _mock_explain_connection(_PLAN_WITH_50TB_LEAF)
    admitted = run_query_bounded("databricks", "SELECT * FROM t", max_estimated_cost_usd=10.0)

    assert admitted.status == "ok"
    assert admitted.estimate.estimated_cost_usd == _EXPECTED_50TB_COST_USD
    mock_execute_bounded.assert_called_once_with("SELECT * FROM t", warehouse=None, max_rows=None)


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine.execute_bounded")
@patch("cost_guard_mcp.engines.databricks._connect")
def test_registered_tool_refuses_databricks_dollar_cap_without_statistics_end_to_end(
    mock_connect, mock_execute_bounded
):
    # Through the registered MCP tool (server.py's @mcp.tool stack and thread dispatch),
    # not just the tools-layer function - the audit reproduced the defect at this layer.
    from cost_guard_mcp.server import mcp

    mock_connect.return_value = _mock_explain_connection(_PLAN_WITHOUT_STATISTICS)

    result = asyncio.run(
        mcp.call_tool(
            "run_query_bounded",
            {"engine": "databricks", "sql": "SELECT * FROM t", "max_estimated_cost_usd": 0.05},
        )
    )

    assert result.structured_content["status"] == "refused"
    assert result.structured_content["reason"] == "cost_cap_exceeded"
    assert result.structured_content["estimate"]["estimated_cost_usd"] is None
    mock_execute_bounded.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine.execute_bounded")
@patch("cost_guard_mcp.engines.databricks._connect")
def test_databricks_iec_sized_plan_refuses_caps_instead_of_admitting_on_a_sub_node(
    mock_connect, mock_execute_bounded
):
    # The audit's check C with the expectation flipped: before the fix this plan parsed to
    # the 20-byte Aggregate node (the GiB leaf was skipped), so a 1 MB byte cap and a
    # 0.05 USD dollar cap both admitted a 134.6 GiB scan. Which cap trips first is the
    # gate's ordering, not this fix's concern - the invariant is that the leaf is seen.
    mock_connect.return_value = _mock_explain_connection(_PLAN_IEC_SHAPE)

    result = run_query_bounded(
        "databricks",
        "SELECT COUNT(*) FROM store_sales",
        max_bytes_billed=1_000_000,
        max_estimated_cost_usd=0.05,
    )

    assert result.status == "refused"
    assert result.reason in {RefusalReason.BYTE_CAP_EXCEEDED, RefusalReason.COST_CAP_EXCEEDED}
    assert result.estimate.estimated_bytes == _IEC_LEAF_BYTES
    mock_execute_bounded.assert_not_called()

    mock_connect.return_value = _mock_explain_connection(_PLAN_IEC_SHAPE)
    byte_cap_only = run_query_bounded(
        "databricks", "SELECT COUNT(*) FROM store_sales", max_bytes_billed=1_000_000
    )

    assert byte_cap_only.status == "refused"
    assert byte_cap_only.reason == RefusalReason.BYTE_CAP_EXCEEDED
    assert byte_cap_only.estimate.estimated_bytes == _IEC_LEAF_BYTES
    mock_execute_bounded.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine.execute_bounded")
@patch("cost_guard_mcp.engines.databricks._connect")
def test_databricks_long_max_sentinel_plan_refuses_both_caps(mock_connect, mock_execute_bounded):
    # Long.MaxValue printed as 8.0 EiB means "statistics unknown": with the None-floor fix
    # in place, both caps must refuse rather than compare against 9.2e18 bytes.
    mock_connect.return_value = _mock_explain_connection(_PLAN_SENTINEL_ONLY)

    result = run_query_bounded(
        "databricks", "SELECT * FROM t", max_bytes_billed=10**18, max_estimated_cost_usd=100.0
    )

    assert result.status == "refused"
    assert result.estimate.estimated_bytes is None
    assert result.estimate.estimated_cost_usd is None
    mock_execute_bounded.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine.execute_bounded")
@patch("cost_guard_mcp.engines.databricks._connect")
def test_run_query_bounded_runs_on_a_stopped_databricks_warehouse_without_caps(
    mock_connect, mock_execute_bounded, databricks_warehouse_lookup_unavailable
):
    # DECISIONS.md #20: the read-only guard refuses on a STOPPED warehouse, but run_query_bounded
    # bypasses it (allow_warehouse_wake=True through the real tools layer) - the estimate's
    # session opens and the execution happens, with the opt-in unset.
    get = databricks_warehouse_lookup_unavailable.workspace_client_cls.return_value.warehouses.get
    get.side_effect = None
    get.return_value = GetWarehouseResponse(
        id="abc123", cluster_size="X-Small", enable_serverless_compute=True, state=State.STOPPED
    )
    mock_connect.return_value = _mock_explain_connection(_PLAN_WITH_50TB_LEAF)
    mock_execute_bounded.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("databricks", "SELECT * FROM t")

    assert result.status == "ok"
    assert result.estimate.estimated_cost_usd == _EXPECTED_50TB_COST_USD
    mock_connect.assert_called_once()
    mock_execute_bounded.assert_called_once_with("SELECT * FROM t", warehouse=None, max_rows=None)
    assert not any("no session was opened" in c for c in result.estimate.caveats)
