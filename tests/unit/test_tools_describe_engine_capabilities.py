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


def test_unsupported_engine_raises_clear_error():
    with pytest.raises(ValueError, match="databricks"):
        describe_engine_capabilities("databricks")


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
