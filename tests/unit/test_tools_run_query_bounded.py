import logging
from unittest.mock import patch

import pytest

from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.tools.run_query_bounded import run_query_bounded
from cost_guard_mcp.types import AccuracyTier, CostEstimate, RefusalReason

_RUN_QUERY_BOUNDED_LOGGER_NAME = "cost_guard_mcp.tools.run_query_bounded"


def _estimate(cost=None, bytes_=None):
    return CostEstimate(
        engine="bigquery",
        accuracy_tier=AccuracyTier.PRECISE,
        estimated_bytes=bytes_,
        estimated_cost_usd=cost,
    )


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_when_cost_cap_exceeded(mock_estimate, caplog):
    mock_estimate.return_value = _estimate(cost=10.00)
    with caplog.at_level(logging.INFO, logger=_RUN_QUERY_BOUNDED_LOGGER_NAME):
        result = run_query_bounded(
            "bigquery", "SELECT * FROM huge_table", max_estimated_cost_usd=1.00
        )
    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED
    assert result.estimate.estimated_cost_usd == 10.00
    assert "1.0" in result.hint or "1.00" in result.hint

    # "Refusal path" logging case: exactly one INFO record naming the engine and the
    # actual refusal reason, with no secret-looking content in it.
    records = [r for r in caplog.records if r.name == _RUN_QUERY_BOUNDED_LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelname == "INFO"
    assert "engine=bigquery" in records[0].message
    assert "reason=cost_cap_exceeded" in records[0].message


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


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_run_query_bounded_dispatches_to_databricks(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("databricks", "SELECT 1")

    mock_engine.execute_bounded.assert_called_once_with("SELECT 1", warehouse=None, max_rows=None)
    assert result.status == "ok"


@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_run_query_bounded_threads_warehouse_size_and_edition_to_cap_check(
    mock_estimate, mock_engine
):
    # Regression test: warehouse_size/edition must reach the cap-check estimate, or
    # max_estimated_cost_usd is always checked against the smallest warehouse's rate
    # regardless of which warehouse the query actually runs on - silently under-enforcing
    # the cap for a caller who specified a larger one.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([], 0, False)

    run_query_bounded(
        "snowflake",
        "SELECT 1",
        warehouse="WH",
        warehouse_size="X6LARGE",
        edition="enterprise",
    )

    mock_estimate.assert_called_once_with("snowflake", "SELECT 1", "WH", "X6LARGE", "enterprise")


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_run_query_bounded_rejects_unsupported_engine(mock_estimate):
    # estimate_query_cost has its OWN identical engine-dispatch else that raises for an
    # invalid engine BEFORE this function's own dispatch block is ever reached - mock it
    # to a non-refusing estimate so execution actually reaches this function's own else
    # branch instead of raising from the wrong function and passing for the wrong reason.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)

    with pytest.raises(UserVisibleError, match="is not yet supported"):
        run_query_bounded("redshift", "SELECT 1")
