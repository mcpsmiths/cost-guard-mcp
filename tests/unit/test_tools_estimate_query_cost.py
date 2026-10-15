from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.tools.estimate_query_cost import estimate_query_cost
from cost_guard_mcp.types import AccuracyTier, CostEstimate


@patch("cost_guard_mcp.tools.estimate_query_cost.bigquery_engine")
def test_estimate_query_cost_dispatches_to_bigquery(mock_engine):
    mock_engine.dry_run.return_value = CostEstimate(
        engine="bigquery",
        accuracy_tier=AccuracyTier.PRECISE,
        estimated_bytes=100,
        estimated_cost_usd=0.0,
    )
    result = estimate_query_cost("bigquery", "SELECT 1")
    mock_engine.dry_run.assert_called_once_with("SELECT 1")
    assert result.engine == "bigquery"


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_estimate_query_cost_bigquery_rls_shape_reports_no_dollar_figure(
    mock_bq_module, _mock_capacity
):
    # #96: the read-only estimate shares dry_run with run_query_bounded's gate, so it shows the
    # same facts - the hidden 0 bytes as reported, HEURISTIC, estimated_cost_usd null and the
    # caveat that says why - instead of a $0.00 PRECISE figure.
    dry_run_job = MagicMock()
    dry_run_job.total_bytes_processed = 0
    dry_run_job._properties = {"statistics": {"query": {"totalBytesProcessedAccuracy": "PRECISE"}}}
    dry_run_job.referenced_tables = [MagicMock()]
    dry_run_job.location = "US"
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = dry_run_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    result = estimate_query_cost("bigquery", "SELECT * FROM rls_protected_table")

    assert result.estimated_bytes == 0
    assert result.estimated_cost_usd is None
    assert result.accuracy_tier == AccuracyTier.HEURISTIC
    assert any("row-level security" in c.lower() for c in result.caveats)


def test_estimate_query_cost_rejects_unsupported_engine():
    with pytest.raises(ValueError, match="redshift"):
        estimate_query_cost("redshift", "SELECT 1")  # type: ignore[arg-type]


@patch("cost_guard_mcp.tools.estimate_query_cost.snowflake_engine")
def test_estimate_query_cost_dispatches_to_snowflake(mock_engine):
    mock_engine.explain_estimate.return_value = CostEstimate(
        engine="snowflake", accuracy_tier=AccuracyTier.UPPER_BOUND, estimated_bytes=100
    )
    result = estimate_query_cost("snowflake", "SELECT 1", warehouse="WH")
    # DECISIONS.md #20: the read-only tool never asks the engine to wake the warehouse.
    mock_engine.explain_estimate.assert_called_once_with(
        "SELECT 1", "WH", allow_warehouse_wake=False
    )
    assert result.engine == "snowflake"


@patch("cost_guard_mcp.tools.estimate_query_cost.databricks_engine")
def test_estimate_query_cost_dispatches_to_databricks(mock_engine):
    mock_engine.explain_estimate.return_value = CostEstimate(
        engine="databricks", accuracy_tier=AccuracyTier.HEURISTIC, estimated_bytes=100
    )
    result = estimate_query_cost("databricks", "SELECT 1", warehouse="ignored")
    mock_engine.explain_estimate.assert_called_once_with(
        "SELECT 1", "ignored", allow_warehouse_wake=False
    )
    assert result.engine == "databricks"


@patch("cost_guard_mcp.tools.estimate_query_cost.snowflake_engine")
def test_estimate_query_cost_threads_warehouse_size_and_edition_to_snowflake(mock_engine):
    # Regression test: warehouse_size/edition must reach the engine, or the dollar estimate
    # always assumes the smallest warehouse regardless of which one the query actually runs on.
    mock_engine.explain_estimate.return_value = CostEstimate(
        engine="snowflake", accuracy_tier=AccuracyTier.UPPER_BOUND, estimated_bytes=100
    )
    estimate_query_cost(
        "snowflake", "SELECT 1", warehouse="WH", warehouse_size="X6LARGE", edition="enterprise"
    )
    mock_engine.explain_estimate.assert_called_once_with(
        "SELECT 1", "WH", allow_warehouse_wake=False, warehouse_size="X6LARGE", edition="enterprise"
    )


@patch("cost_guard_mcp.tools.estimate_query_cost.databricks_engine")
def test_estimate_query_cost_threads_warehouse_size_to_databricks(mock_engine):
    mock_engine.explain_estimate.return_value = CostEstimate(
        engine="databricks", accuracy_tier=AccuracyTier.HEURISTIC, estimated_bytes=100
    )
    estimate_query_cost("databricks", "SELECT 1", warehouse_size="4X-Large")
    mock_engine.explain_estimate.assert_called_once_with(
        "SELECT 1", None, allow_warehouse_wake=False, warehouse_size="4X-Large"
    )


@pytest.mark.parametrize("engine", ["bigquery", "snowflake", "databricks"])
@patch("cost_guard_mcp.tools.estimate_query_cost.databricks_engine")
@patch("cost_guard_mcp.tools.estimate_query_cost.snowflake_engine")
@patch("cost_guard_mcp.tools.estimate_query_cost.bigquery_engine")
def test_estimate_query_cost_refuses_multi_statement_text_before_any_engine_call(
    mock_bigquery, mock_snowflake, mock_databricks, engine
):
    # estimate_query_cost is the read_only_hint=True tool and the estimate run_query_bounded
    # gates on - a dry run / EXPLAIN of multi-statement text prices an unknown subset of it.
    with pytest.raises(UserVisibleError, match="exactly one SQL statement"):
        estimate_query_cost(engine, "SELECT 1; DELETE FROM big_t WHERE TRUE")  # type: ignore[arg-type]

    mock_bigquery.dry_run.assert_not_called()
    mock_snowflake.explain_estimate.assert_not_called()
    mock_databricks.explain_estimate.assert_not_called()


@pytest.mark.parametrize("engine", ["bigquery", "snowflake", "databricks"])
@patch("cost_guard_mcp.tools.estimate_query_cost.databricks_engine")
@patch("cost_guard_mcp.tools.estimate_query_cost.snowflake_engine")
@patch("cost_guard_mcp.tools.estimate_query_cost.bigquery_engine")
def test_estimate_query_cost_refuses_a_write_statement_before_any_engine_call(
    mock_bigquery, mock_snowflake, mock_databricks, engine
):
    # DECISIONS.md #13: the read gate runs right after the single-statement gate, before the
    # dry run / EXPLAIN, and names the operator opt-out.
    with pytest.raises(UserVisibleError, match="only read statements") as excinfo:
        estimate_query_cost(engine, "DELETE FROM big_t WHERE TRUE")  # type: ignore[arg-type]

    assert "COST_GUARD_ALLOW_WRITE_STATEMENTS=1" in str(excinfo.value)
    mock_bigquery.dry_run.assert_not_called()
    mock_snowflake.explain_estimate.assert_not_called()
    mock_databricks.explain_estimate.assert_not_called()


# --- DECISIONS.md #20: the warehouse-wake flag is threaded to the warehouse engines only -------


@patch("cost_guard_mcp.tools.estimate_query_cost.databricks_engine")
@patch("cost_guard_mcp.tools.estimate_query_cost.snowflake_engine")
@patch("cost_guard_mcp.tools.estimate_query_cost.bigquery_engine")
def test_read_only_estimates_do_not_ask_to_wake_the_warehouse(
    mock_bigquery, mock_snowflake, mock_databricks
):
    mock_snowflake.explain_estimate.return_value = CostEstimate(
        engine="snowflake", accuracy_tier=AccuracyTier.UPPER_BOUND, estimated_bytes=1
    )
    mock_databricks.explain_estimate.return_value = CostEstimate(
        engine="databricks", accuracy_tier=AccuracyTier.HEURISTIC, estimated_bytes=1
    )
    mock_bigquery.dry_run.return_value = CostEstimate(
        engine="bigquery", accuracy_tier=AccuracyTier.PRECISE, estimated_bytes=1
    )

    estimate_query_cost("snowflake", "SELECT 1", warehouse="WH")
    estimate_query_cost("databricks", "SELECT 1")
    estimate_query_cost("bigquery", "SELECT 1")

    mock_snowflake.explain_estimate.assert_called_once_with(
        "SELECT 1", "WH", allow_warehouse_wake=False
    )
    mock_databricks.explain_estimate.assert_called_once_with(
        "SELECT 1", None, allow_warehouse_wake=False
    )
    mock_bigquery.dry_run.assert_called_once_with("SELECT 1")


@patch("cost_guard_mcp.tools.estimate_query_cost.databricks_engine")
@patch("cost_guard_mcp.tools.estimate_query_cost.snowflake_engine")
def test_allow_warehouse_wake_is_threaded_to_the_warehouse_engines(mock_snowflake, mock_databricks):
    mock_snowflake.explain_estimate.return_value = CostEstimate(
        engine="snowflake", accuracy_tier=AccuracyTier.UPPER_BOUND, estimated_bytes=1
    )
    mock_databricks.explain_estimate.return_value = CostEstimate(
        engine="databricks", accuracy_tier=AccuracyTier.HEURISTIC, estimated_bytes=1
    )

    estimate_query_cost("snowflake", "SELECT 1", warehouse="WH", allow_warehouse_wake=True)
    estimate_query_cost("databricks", "SELECT 1", warehouse_size="Small", allow_warehouse_wake=True)

    mock_snowflake.explain_estimate.assert_called_once_with(
        "SELECT 1", "WH", allow_warehouse_wake=True
    )
    mock_databricks.explain_estimate.assert_called_once_with(
        "SELECT 1", None, allow_warehouse_wake=True, warehouse_size="Small"
    )


def test_allow_warehouse_wake_is_keyword_only():
    # It must never be passable positionally as a tool argument (same reasoning as
    # before_execute): the published tool signatures do not carry it.
    import inspect

    parameter = inspect.signature(estimate_query_cost).parameters["allow_warehouse_wake"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False
