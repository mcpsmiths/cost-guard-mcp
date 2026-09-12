import pytest
from pydantic import ValidationError

from cost_guard_mcp.types import (
    AccuracyTier,
    BoundedQueryResult,
    CostEstimate,
    EngineCapabilities,
    RefusalReason,
)


def test_cost_estimate_requires_accuracy_tier():
    with pytest.raises(ValidationError):
        CostEstimate(engine="bigquery", estimated_bytes=1024)


def test_cost_estimate_accepts_valid_precise_estimate():
    est = CostEstimate(
        engine="bigquery",
        accuracy_tier=AccuracyTier.PRECISE,
        estimated_bytes=1_099_511_627_776,
        estimated_cost_usd=6.25,
    )
    assert est.accuracy_tier == AccuracyTier.PRECISE
    assert est.currency == "USD"
    assert est.caveats == []


def test_cost_estimate_rejects_invalid_tier_string():
    with pytest.raises(ValidationError):
        CostEstimate(engine="bigquery", accuracy_tier="EXACT", estimated_bytes=1)


def test_engine_capabilities_round_trip():
    caps = EngineCapabilities(
        engine="snowflake",
        supports_precise_bytes=False,
        supports_dollar_estimate=True,
        default_accuracy_tier=AccuracyTier.UPPER_BOUND,
        known_gaps=["Cortex AI Function cost is not captured"],
    )
    assert caps.default_accuracy_tier == AccuracyTier.UPPER_BOUND
    assert "Cortex AI Function cost is not captured" in caps.known_gaps


def test_bounded_query_result_refused_shape():
    result = BoundedQueryResult(
        status="refused",
        reason=RefusalReason.COST_CAP_EXCEEDED,
        estimate=CostEstimate(
            engine="bigquery", accuracy_tier=AccuracyTier.PRECISE, estimated_cost_usd=12.50
        ),
        hint="Narrow your WHERE clause or raise max_estimated_cost_usd.",
    )
    assert result.status == "refused"
    assert result.rows is None
    assert result.row_count is None
