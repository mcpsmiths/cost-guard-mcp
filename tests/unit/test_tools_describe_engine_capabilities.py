import pytest

from cost_guard_mcp.tools.describe_engine_capabilities import describe_engine_capabilities
from cost_guard_mcp.types import AccuracyTier


def test_bigquery_capabilities_are_precise_by_default():
    caps = describe_engine_capabilities("bigquery")
    assert caps.engine == "bigquery"
    assert caps.supports_precise_bytes is True
    assert caps.supports_dollar_estimate is True
    assert caps.default_accuracy_tier == AccuracyTier.PRECISE
    assert len(caps.known_gaps) >= 1


def test_unknown_engine_string_raises_clear_error():
    with pytest.raises(ValueError, match="not yet supported"):
        describe_engine_capabilities("redshift")  # type: ignore[arg-type]


def test_snowflake_capabilities_are_upper_bound_by_default():
    caps = describe_engine_capabilities("snowflake")
    assert caps.engine == "snowflake"
    assert caps.supports_precise_bytes is False
    assert caps.supports_dollar_estimate is True
    assert caps.default_accuracy_tier == AccuracyTier.UPPER_BOUND
    assert any("Cortex" in gap for gap in caps.known_gaps)


def test_databricks_capabilities_are_heuristic_by_default():
    caps = describe_engine_capabilities("databricks")
    assert caps.engine == "databricks"
    assert caps.supports_precise_bytes is False
    assert caps.supports_dollar_estimate is True
    assert caps.default_accuracy_tier == AccuracyTier.HEURISTIC
    assert any("EXPLAIN COST" in gap for gap in caps.known_gaps)


def test_databricks_known_gaps_disclose_dollar_estimate_is_none_without_statistics():
    # Audit: databricks-dollar-estimate-floor-when-size-statistics-absent - the gap text
    # must tell the caller that BOTH the byte and the dollar estimate are unavailable
    # without plan statistics and that run_query_bounded refuses caps in that state.
    caps = describe_engine_capabilities("databricks")
    assert any("dollar" in gap and "unavailable" in gap for gap in caps.known_gaps)
    assert any("refuse" in gap.lower() for gap in caps.known_gaps)


def test_snowflake_capabilities_disclose_observed_size_reconciliation_and_fail_closed_types():
    caps = describe_engine_capabilities("snowflake")
    assert any("SHOW WAREHOUSES" in gap and "only raise" in gap for gap in caps.known_gaps)
    assert any(
        "Snowpark-optimized" in gap and "no dollar" in gap.lower() for gap in caps.known_gaps
    )


def test_databricks_capabilities_disclose_observed_size_reconciliation_and_fail_closed_types():
    caps = describe_engine_capabilities("databricks")
    assert any("only raise" in gap for gap in caps.known_gaps)
    assert any("Classic/Pro" in gap and "no dollar" in gap.lower() for gap in caps.known_gaps)
