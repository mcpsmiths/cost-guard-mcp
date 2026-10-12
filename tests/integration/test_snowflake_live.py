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


def test_lookup_warehouse_facts_reads_the_live_show_warehouses_row_shape():
    # Release gate for observed-warehouse pricing (DECISIONS.md #19): the SHOW WAREHOUSES
    # `size`/`type`/`generation` text must map onto the pricing table on a real account, and
    # CURRENT_WAREHOUSE() must resolve the session's warehouse when no argument is given.
    from cost_guard_mcp.engines.snowflake import _connect, _lookup_warehouse_facts
    from cost_guard_mcp.pricing.snowflake_pricing import credits_per_hour

    _require_env("SNOWFLAKE_ACCOUNT")
    _require_env("SNOWFLAKE_USER")
    _require_env("SNOWFLAKE_ROLE")

    conn = _connect()
    try:
        facts = _lookup_warehouse_facts(conn, "COMPUTE_WH")
        with conn.cursor() as cur:
            cur.execute("USE WAREHOUSE COMPUTE_WH")
        resolved = _lookup_warehouse_facts(conn, None)
    finally:
        conn.close()

    assert facts is not None
    assert facts.name == "COMPUTE_WH"
    assert facts.warehouse_type == "STANDARD"
    assert facts.size_key is not None, facts.size_text
    assert credits_per_hour(facts.size_key) > 0
    assert facts.generation in ("1", "2")
    assert facts.cloud_provider in ("AWS", "AZURE", "GCP")
    assert facts.state is not None
    assert resolved is not None
    assert resolved.name == "COMPUTE_WH"
