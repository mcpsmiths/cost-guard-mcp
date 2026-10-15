"""Live integration tests against a real BigQuery Sandbox project.

Requires BIGQUERY_PROJECT and GOOGLE_APPLICATION_CREDENTIALS to be set. Hard-fails
(rather than skips) if unset, matching googleapis/mcp-toolbox's own convention — this
test tier is meant to run ONLY via `workflow_dispatch`/scheduled CI, never by accident.
"""

import os

import pytest

from cost_guard_mcp.types import AccuracyTier, RefusalReason

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

    _require_env("BIGQUERY_PROJECT")
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


def test_run_query_bounded_default_limit_wrap_rejects_duplicate_output_columns():
    """#94: with max_rows omitted a single SELECT runs inside `SELECT * FROM (...) AS
    cost_guard_row_cap LIMIT 1001`, so it must be valid as a derived table. BigQuery accepts
    duplicate output column names at the top level (the dry run prices the unwrapped text and
    passes) but rejects them inside a subquery with "Duplicate column names in the result are
    not supported"; the job fails at compile time, nothing is billed. This pins the real provider
    behaviour behind the README / tool-description remedy (alias colliding output columns). If
    BigQuery ever accepts the wrapped form, this test fails and the remedy text comes out."""
    from cost_guard_mcp.errors import SanitizedEngineError
    from cost_guard_mcp.tools.run_query_bounded import run_query_bounded

    _require_env("BIGQUERY_PROJECT")
    _require_env("GOOGLE_APPLICATION_CREDENTIALS")

    with pytest.raises(SanitizedEngineError, match="(?i)duplicate column"):
        run_query_bounded("bigquery", "SELECT 1 AS a, 2 AS a")

    # The remedy, under the same default wrap.
    result = run_query_bounded("bigquery", "SELECT 1 AS a, 2 AS b")
    assert result.status == "ok"
    assert result.rows == [{"a": 1, "b": 2}]


def test_run_query_bounded_default_limit_wrap_accepts_a_recursive_cte():
    """#94 fix-up: is_single_query_statement admits WITH RECURSIVE, so with max_rows omitted the
    whole query runs inside `SELECT * FROM (WITH RECURSIVE ...) AS cost_guard_row_cap LIMIT 1001`.
    Whether BigQuery accepts a recursive CTE inside a derived table is a provider fact the unit
    suite cannot pin; this case does (unverified at merge time - flagged by the fix-up review).
    If it fails with a compile-time error (nothing billed), the wrap rule must exclude recursive
    CTEs or the README / tool description must say to put the LIMIT inside such a query."""
    from cost_guard_mcp.tools.run_query_bounded import run_query_bounded

    _require_env("BIGQUERY_PROJECT")
    _require_env("GOOGLE_APPLICATION_CREDENTIALS")

    result = run_query_bounded(
        "bigquery",
        "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 5) "
        "SELECT n FROM t",
    )
    assert result.status == "ok"
    assert result.executed is True
    assert sorted(row["n"] for row in result.rows) == [1, 2, 3, 4, 5]


def test_run_query_bounded_limit_wrap_preserves_an_inner_order_by():
    """#94 fix-up: before the default wrap an ORDER BY query without LIMIT returned its ordered
    head; now it runs as `SELECT * FROM (... ORDER BY n DESC) AS cost_guard_row_cap LIMIT 11`,
    and whether the engine honours the inner ORDER BY when it applies the outer LIMIT is a
    provider fact only a live run can pin (unverified at merge time - flagged by the fix-up
    review). If this fails the rows are an engine-dependent prefix: extend the tool
    description's derived-table sentence with "put the LIMIT inside your query when order
    matters" and record it in DECISIONS.md #21."""
    from cost_guard_mcp.tools.run_query_bounded import run_query_bounded

    _require_env("BIGQUERY_PROJECT")
    _require_env("GOOGLE_APPLICATION_CREDENTIALS")

    result = run_query_bounded(
        "bigquery",
        "SELECT n FROM UNNEST(GENERATE_ARRAY(1, 1500)) AS n ORDER BY n DESC",
        max_rows=10,
    )
    # 1500 rows against max_rows=10: the sentinel row is seen, so this is the billed truncation
    # refusal, and its ten rows must be the ten largest in descending order.
    assert result.status == "refused"
    assert result.reason == RefusalReason.ROW_CAP_EXCEEDED
    assert result.executed is True
    assert [row["n"] for row in result.rows] == list(range(1500, 1490, -1))
