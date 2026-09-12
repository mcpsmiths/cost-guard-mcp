"""Live integration tests against a real BigQuery Sandbox project.

Requires BIGQUERY_PROJECT and GOOGLE_APPLICATION_CREDENTIALS to be set. Hard-fails
(rather than skips) if unset, matching googleapis/mcp-toolbox's own convention — this
test tier is meant to run ONLY via `workflow_dispatch`/scheduled CI, never by accident.
"""

import os

import pytest

from cost_guard_mcp.types import AccuracyTier

pytestmark = pytest.mark.integration


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be set to run BigQuery live integration tests.")
    return value


def test_dry_run_against_public_dataset():
    from cost_guard_mcp.engines.bigquery import dry_run

    project = _require_env("BIGQUERY_PROJECT")
    _require_env("GOOGLE_APPLICATION_CREDENTIALS")

    sql = (
        "SELECT name, SUM(number) AS total "
        "FROM `bigquery-public-data.usa_names.usa_1910_2013` "
        "GROUP BY name ORDER BY total DESC LIMIT 10"
    )
    estimate = dry_run(sql, project=project)

    assert estimate.accuracy_tier == AccuracyTier.PRECISE
    assert estimate.estimated_bytes > 0
    assert estimate.estimated_cost_usd is not None
    assert estimate.estimated_cost_usd >= 0


def test_run_query_bounded_refuses_when_cap_too_low():
    from cost_guard_mcp.tools.run_query_bounded import run_query_bounded

    project = _require_env("BIGQUERY_PROJECT")
    _require_env("GOOGLE_APPLICATION_CREDENTIALS")

    sql = "SELECT * FROM `bigquery-public-data.usa_names.usa_1910_2013`"
    result = run_query_bounded(
        "bigquery", sql, max_bytes_billed=1
    )  # 1 byte cap -> guaranteed refusal

    assert result.status == "refused"
    assert result.reason.value == "byte_cap_exceeded"
    assert result.rows is None


def test_run_query_bounded_succeeds_under_generous_cap():
    from cost_guard_mcp.tools.run_query_bounded import run_query_bounded

    _require_env("BIGQUERY_PROJECT")
    _require_env("GOOGLE_APPLICATION_CREDENTIALS")

    sql = (
        "SELECT name, SUM(number) AS total FROM `bigquery-public-data.usa_names.usa_1910_2013` "
        "GROUP BY name ORDER BY total DESC LIMIT 5"
    )
    result = run_query_bounded(
        "bigquery", sql, max_bytes_billed=10_000_000_000, max_rows=10, max_estimated_cost_usd=1.00
    )

    assert result.status == "ok"
    assert result.row_count == 5
    assert len(result.rows) == 5
