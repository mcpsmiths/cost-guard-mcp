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


def test_run_query_bounded_default_limit_wrap_accepts_duplicate_output_columns():
    """#94 / #124: with max_rows omitted a single SELECT runs inside `SELECT * FROM (...) AS
    cost_guard_row_cap LIMIT 1001`, so it must be valid as a derived table. The 0.6.0 pre-tag live
    gate (integration.yml run 39275182240) showed BigQuery ACCEPTS a duplicate output column name
    inside the wrap: the earlier expectation of "Duplicate column names in the result are not
    supported" reported DID NOT RAISE, and the remedy "alias them" was withdrawn from the README,
    the tool description and the release notes (DECISIONS.md #21, amended 2026-10-19). This pins
    the observed behaviour; every assertion message prints the rows so the next run records which
    column names BigQuery returns for the wrapped form (not observed in that run). If BigQuery ever
    refuses the wrapped form, this fails and the remedy comes back - with evidence this time."""
    from cost_guard_mcp.tools.run_query_bounded import run_query_bounded

    _require_env("BIGQUERY_PROJECT")
    _require_env("GOOGLE_APPLICATION_CREDENTIALS")

    result = run_query_bounded("bigquery", "SELECT 1 AS a, 2 AS a")

    assert result.status == "ok", (
        f"status={result.status!r} reason={result.reason!r} rows={result.rows!r}"
    )
    assert result.executed is True, f"executed={result.executed!r} rows={result.rows!r}"
    assert result.row_count == 1, f"row_count={result.row_count!r} rows={result.rows!r}"
    assert len(result.rows) == 1, f"rows={result.rows!r}"

    # Control: distinct names under the same default wrap.
    result = run_query_bounded("bigquery", "SELECT 1 AS a, 2 AS b")
    assert result.status == "ok", f"status={result.status!r} rows={result.rows!r}"
    assert result.rows == [{"a": 1, "b": 2}], f"rows={result.rows!r}"


def test_run_query_bounded_default_limit_wrap_accepts_a_recursive_cte():
    """#124: in the 0.6.0 pre-tag live gate (integration.yml run 39275182240) this query, wrapped as
    `SELECT * FROM (WITH RECURSIVE ...) AS cost_guard_row_cap LIMIT 1001`, was refused by BigQuery
    at compile time, nothing billed: "400 WITH RECURSIVE is only allowed at the top level of the
    SELECT, CREATE TABLE AS SELECT, CREATE VIEW, INSERT, EXPORT DATA statements." The wrap rule
    (sql_scope_keywords.is_single_query_statement) now excludes a recursive main WITH, so the text
    reaches BigQuery unchanged and the paged fetch (max_rows + 1 sentinel) is its row cap, as for
    SHOW and scripts. This pins that the unwrapped run succeeds under the default cap with the full
    result; if it fails with that 400 again, the wrap rule has regressed."""
    from cost_guard_mcp.tools.run_query_bounded import run_query_bounded

    _require_env("BIGQUERY_PROJECT")
    _require_env("GOOGLE_APPLICATION_CREDENTIALS")

    result = run_query_bounded(
        "bigquery",
        "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 5) "
        "SELECT n FROM t",
    )
    assert result.status == "ok", f"status={result.status!r} reason={result.reason!r}"
    assert result.executed is True
    assert result.row_count == 5
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
