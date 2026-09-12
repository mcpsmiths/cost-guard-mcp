from unittest.mock import patch

from cost_guard_mcp.tools.run_query_bounded import run_query_bounded
from cost_guard_mcp.types import AccuracyTier, CostEstimate, RefusalReason


def _estimate(cost=None, bytes_=None):
    return CostEstimate(
        engine="bigquery",
        accuracy_tier=AccuracyTier.PRECISE,
        estimated_bytes=bytes_,
        estimated_cost_usd=cost,
    )


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_when_cost_cap_exceeded(mock_estimate):
    mock_estimate.return_value = _estimate(cost=10.00)
    result = run_query_bounded("bigquery", "SELECT * FROM huge_table", max_estimated_cost_usd=1.00)
    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED
    assert result.estimate.estimated_cost_usd == 10.00
    assert "1.0" in result.hint or "1.00" in result.hint


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_when_byte_cap_exceeded(mock_estimate):
    mock_estimate.return_value = _estimate(bytes_=10_000_000_000)
    result = run_query_bounded("bigquery", "SELECT * FROM t", max_bytes_billed=1_000_000)
    assert result.status == "refused"
    assert result.reason == RefusalReason.BYTE_CAP_EXCEEDED


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_fail_closed_when_cost_cap_requested_but_estimate_unavailable(mock_estimate):
    mock_estimate.return_value = _estimate(cost=None)
    result = run_query_bounded("bigquery", "SELECT * FROM t", max_estimated_cost_usd=1.00)
    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_fail_closed_when_byte_cap_requested_but_estimate_unavailable(mock_estimate):
    mock_estimate.return_value = _estimate(bytes_=None)
    result = run_query_bounded("bigquery", "SELECT * FROM t", max_bytes_billed=1_000_000)
    assert result.status == "refused"
    assert result.reason == RefusalReason.BYTE_CAP_EXCEEDED


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_succeeds_when_under_all_caps(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded(
        "bigquery", "SELECT 1", max_estimated_cost_usd=1.00, max_bytes_billed=1_000_000, max_rows=10
    )

    assert result.status == "ok"
    assert result.row_count == 1
    assert result.rows == [{"a": 1}]


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_after_execution_when_row_cap_hit(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}, {"a": 2}], 2, True)  # row_cap_hit=True

    result = run_query_bounded("bigquery", "SELECT * FROM t", max_rows=2)

    assert result.status == "refused"
    assert result.reason == RefusalReason.ROW_CAP_EXCEEDED
    assert result.row_count == 2
    assert result.rows == [{"a": 1}, {"a": 2}]  # still returns the (capped) rows it did fetch
