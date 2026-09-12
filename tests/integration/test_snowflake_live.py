"""Live integration tests against a real Snowflake trial account.

Requires SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_ROLE, and either
SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PASSWORD. Hard-fails if unset — this tier runs
only via workflow_dispatch/scheduled CI.
"""

import os

import pytest

from cost_guard_mcp.types import AccuracyTier

pytestmark = pytest.mark.integration


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be set to run Snowflake live integration tests.")
    return value


def test_explain_estimate_against_trial_warehouse():
    from cost_guard_mcp.engines.snowflake import explain_estimate

    _require_env("SNOWFLAKE_ACCOUNT")
    _require_env("SNOWFLAKE_USER")
    _require_env("SNOWFLAKE_ROLE")

    estimate = explain_estimate(
        "SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER LIMIT 100",
        warehouse="COMPUTE_WH",
        warehouse_size="XSMALL",
    )

    assert estimate.accuracy_tier == AccuracyTier.UPPER_BOUND
    assert estimate.estimated_bytes is not None
    assert estimate.estimated_cost_usd is not None


def test_run_query_bounded_refuses_on_impossible_byte_cap():
    from cost_guard_mcp.tools.run_query_bounded import run_query_bounded

    result = run_query_bounded(
        "snowflake",
        "SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER",
        warehouse="COMPUTE_WH",
        max_bytes_billed=1,
    )

    assert result.status == "refused"
