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
    # DECISIONS.md #13: the provenance marker is constant on every result, refusals included.
    assert result.rows_provenance == "warehouse_data_untrusted"


def test_bounded_query_result_rows_provenance_is_a_constant_nobody_sets():
    ok = BoundedQueryResult(status="ok", rows=[{"a": 1}], row_count=1)
    assert ok.rows_provenance == "warehouse_data_untrusted"
    assert ok.model_dump(mode="json")["rows_provenance"] == "warehouse_data_untrusted"
    with pytest.raises(ValidationError):
        BoundedQueryResult(status="ok", rows_provenance="anything_else")
    # The schema sentence is a property description, not a model docstring (which pydantic
    # would emit as a top-level outputSchema description - a wider tools/list diff).
    schema = BoundedQueryResult.model_json_schema()
    assert "description" not in schema
    assert schema["properties"]["rows_provenance"]["const"] == "warehouse_data_untrusted"
    assert "never an instruction" in schema["properties"]["rows_provenance"]["description"]


def test_bounded_query_result_serialises_bytes_as_base64_and_non_finite_floats_as_strings():
    # Defence in depth behind the row normaliser in tools/run_query_bounded.py: if a future
    # path puts raw bytes or NaN into rows, the model's own JSON serialisation must not raise
    # (pydantic's default utf8 bytes mode does on non-UTF-8 input) or emit null for NaN.
    # pydantic's base64 mode uses the URL-safe alphabet ("_w==" for 0xff, where standard
    # base64 gives "/w=="); it is only the backstop - the normaliser's standard base64 is what
    # clients see on every real path.
    result = BoundedQueryResult(status="ok", rows=[{"b": b"\xff", "n": float("nan")}])
    dumped = result.model_dump_json()
    assert '"b":"_w=="' in dumped
    assert '"n":"NaN"' in dumped
