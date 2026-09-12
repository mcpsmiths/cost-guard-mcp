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
    with pytest.raises(ValueError, match="databricks"):
        estimate_query_cost("databricks", "SELECT 1")
