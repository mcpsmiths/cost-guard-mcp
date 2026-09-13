from unittest.mock import patch

import pytest

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


def test_estimate_query_cost_rejects_unsupported_engine():
    with pytest.raises(ValueError, match="redshift"):
        estimate_query_cost("redshift", "SELECT 1")  # type: ignore[arg-type]


@patch("cost_guard_mcp.tools.estimate_query_cost.snowflake_engine")
def test_estimate_query_cost_dispatches_to_snowflake(mock_engine):
    mock_engine.explain_estimate.return_value = CostEstimate(
        engine="snowflake", accuracy_tier=AccuracyTier.UPPER_BOUND, estimated_bytes=100
    )
    result = estimate_query_cost("snowflake", "SELECT 1", warehouse="WH")
    mock_engine.explain_estimate.assert_called_once_with("SELECT 1", "WH")
    assert result.engine == "snowflake"


@patch("cost_guard_mcp.tools.estimate_query_cost.databricks_engine")
def test_estimate_query_cost_dispatches_to_databricks(mock_engine):
    mock_engine.explain_estimate.return_value = CostEstimate(
        engine="databricks", accuracy_tier=AccuracyTier.HEURISTIC, estimated_bytes=100
    )
    result = estimate_query_cost("databricks", "SELECT 1", warehouse="ignored")
    mock_engine.explain_estimate.assert_called_once_with("SELECT 1", "ignored")
    assert result.engine == "databricks"
