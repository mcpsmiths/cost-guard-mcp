"""Gate-level regression tests for the Databricks leg of run_query_bounded.

These exercise the real engines/databricks.py explain_estimate (only the connector is
mocked) through tools/run_query_bounded.py, so they prove the fail-closed cap gate sees
what the engine really emits - not a hand-built CostEstimate. They live in their own
module rather than test_tools_run_query_bounded.py so that file keeps a single writer.
"""

import asyncio
from unittest.mock import MagicMock, patch

from cost_guard_mcp.tools.run_query_bounded import run_query_bounded
from cost_guard_mcp.types import RefusalReason

# Audit: databricks-dollar-estimate-floor-when-size-statistics-absent
_PLAN_WITHOUT_STATISTICS = [("== Optimized Logical Plan ==",), ("Relation[a,b] csv",)]
# 50 TB on the default X-Small Serverless warehouse: 6 DBU/h * 0.70 USD * (30 s * 100x) = 3.5 USD.
_PLAN_WITH_50TB_LEAF = [("Relation[a] parquet, Statistics(sizeInBytes=50.0 TB, rowCount=1)",)]
_EXPECTED_50TB_COST_USD = 3.5


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
