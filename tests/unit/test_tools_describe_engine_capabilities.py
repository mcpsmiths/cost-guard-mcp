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


@pytest.mark.parametrize("engine", ["snowflake", "databricks"])
def test_warehouse_engines_disclose_that_read_only_tools_can_start_or_resume_a_warehouse(engine):
    caps = describe_engine_capabilities(engine)
    assert any(
        "check_credentials" in gap and "estimate_query_cost" in gap and "compute" in gap
        for gap in caps.known_gaps
    )


def test_databricks_discloses_the_state_guard_and_its_opt_in():
    # DECISIONS.md #20: the gap names the default (refuse unless RUNNING, fail closed when
    # unreadable), the exact opt-in, and that run_query_bounded is not gated.
    caps = describe_engine_capabilities("databricks")
    guard_gap = next(gap for gap in caps.known_gaps if "run_query_bounded is not gated" in gap)
    assert "COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true" in guard_gap
    assert "RUNNING" in guard_gap
    assert "check_credentials" in guard_gap and "estimate_query_cost" in guard_gap
    assert "compute" in guard_gap
    assert "run_query_bounded is not gated" in guard_gap
    assert "cannot be read" in guard_gap


def test_databricks_no_longer_promises_the_lookup_fallback_unconditionally():
    # The caller-asserted-size fallback on a failed lookup now exists only for
    # run_query_bounded or under the opt-in; every gap that mentions it must say so.
    caps = describe_engine_capabilities("databricks")
    fallback_gaps = [gap for gap in caps.known_gaps if "falls back to the caller-asserted" in gap]
    assert fallback_gaps
    for gap in fallback_gaps:
        assert "run_query_bounded" in gap
        assert "COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true" in gap
        assert "by default" in gap and "refuse" in gap


@pytest.mark.parametrize(
    ("engine", "variable", "running_state"),
    [
        ("snowflake", "COST_GUARD_SNOWFLAKE_ALLOW_WAREHOUSE_RESUME", "STARTED"),
        ("databricks", "COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START", "RUNNING"),
    ],
)
def test_warehouse_engines_disclose_the_state_guard_and_its_opt_in(engine, variable, running_state):
    caps = describe_engine_capabilities(engine)
    guard_gap = next(gap for gap in caps.known_gaps if "run_query_bounded is not gated" in gap)
    assert f"{variable}=true" in guard_gap
    assert running_state in guard_gap
    assert "check_credentials" in guard_gap and "estimate_query_cost" in guard_gap
    assert "compute" in guard_gap


def test_snowflake_discloses_which_statements_the_guard_skips_and_which_always_run():
    caps = describe_engine_capabilities("snowflake")
    guard_gap = next(gap for gap in caps.known_gaps if "run_query_bounded is not gated" in gap)
    assert "CURRENT_REGION()" in guard_gap and "QUERY_HISTORY" in guard_gap
    assert "SELECT CURRENT_WAREHOUSE()" in guard_gap
    assert "no warehouse argument" in guard_gap
    assert "ok=true" in guard_gap
