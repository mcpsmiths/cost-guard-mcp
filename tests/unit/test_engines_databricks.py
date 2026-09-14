import time
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.engines.databricks import _connect, check_credentials

# Placeholder values for mocked config fields below - never real credentials.
_FAKE_PAT = "fake-pat-placeholder"
_FAKE_OAUTH_CREDENTIAL_VALUE = "fake-oauth-credential-placeholder"


@patch("cost_guard_mcp.engines.databricks.sql.connect")
@patch("cost_guard_mcp.engines.databricks.load_databricks_config")
def test_connect_uses_access_token_when_pat_set(mock_load_config, mock_connect):
    mock_load_config.return_value = MagicMock(
        server_hostname="my-workspace.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        access_token=_FAKE_PAT,
        client_id=None,
        client_secret=None,
    )

    _connect()

    mock_connect.assert_called_once_with(
        server_hostname="my-workspace.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        access_token=_FAKE_PAT,
        _socket_timeout=60,
    )


@patch("cost_guard_mcp.engines.databricks.oauth_service_principal")
@patch("cost_guard_mcp.engines.databricks.Config")
@patch("cost_guard_mcp.engines.databricks.sql.connect")
@patch("cost_guard_mcp.engines.databricks.load_databricks_config")
def test_connect_uses_oauth_m2m_when_client_credentials_set(
    mock_load_config, mock_connect, mock_config_cls, mock_oauth_service_principal
):
    mock_load_config.return_value = MagicMock(
        server_hostname="my-workspace.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        access_token=None,
        client_id="client-abc",
        client_secret=_FAKE_OAUTH_CREDENTIAL_VALUE,
    )
    mock_sdk_config = MagicMock()
    mock_config_cls.return_value = mock_sdk_config

    _connect()

    mock_config_cls.assert_called_once_with(
        host="https://my-workspace.cloud.databricks.com",
        client_id="client-abc",
        client_secret=_FAKE_OAUTH_CREDENTIAL_VALUE,
    )
    _, kwargs = mock_connect.call_args
    assert kwargs["server_hostname"] == "my-workspace.cloud.databricks.com"
    assert kwargs["http_path"] == "/sql/1.0/warehouses/abc123"
    assert kwargs["_socket_timeout"] == 60
    assert callable(kwargs["credentials_provider"])
    kwargs["credentials_provider"]()
    mock_oauth_service_principal.assert_called_once_with(mock_sdk_config)


@patch("cost_guard_mcp.engines.databricks._connect")
def test_check_credentials_ok_when_query_succeeds(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("alice@example.com",)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials()

    mock_cursor.execute.assert_called_once_with("SELECT current_user()")
    assert result.engine == "databricks"
    assert result.ok is True
    assert "alice@example.com" in result.detail


@patch("cost_guard_mcp.engines.databricks._connect")
def test_check_credentials_reports_failure_as_data_not_a_raise(mock_connect):
    mock_connect.side_effect = Exception("401 Unauthorized")

    result = check_credentials()

    assert result.engine == "databricks"
    assert result.ok is False
    assert "401 Unauthorized" in result.detail


@patch("cost_guard_mcp.engines.databricks._connect")
def test_check_credentials_closes_connection_on_success(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("alice@example.com",)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials()

    assert result.ok is True
    mock_conn.close.assert_called_once()


@patch("cost_guard_mcp.engines.databricks._connect")
def test_check_credentials_closes_connection_even_on_failure(mock_connect):
    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = RuntimeError("boom")
    mock_connect.return_value = mock_conn

    result = check_credentials()

    assert result.ok is False
    assert "boom" in result.detail
    mock_conn.close.assert_called_once()


@patch("cost_guard_mcp.engines.databricks._connect")
def test_check_credentials_ignores_warehouse_parameter(mock_connect):
    # Databricks has no per-query USE WAREHOUSE - the warehouse parameter exists only
    # for calling-convention consistency with bigquery/snowflake and is a documented
    # no-op here.
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("alice@example.com",)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials(warehouse="some-warehouse-name")

    assert result.ok is True
    mock_cursor.execute.assert_called_once_with("SELECT current_user()")


from cost_guard_mcp.engines.databricks import explain_estimate
from cost_guard_mcp.types import AccuracyTier


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_parses_max_size_in_bytes_from_statistics(mock_connect):
    # Real confirmed EXPLAIN COST output shape (docs.databricks.com/aws/en/optimizations/cbo,
    # live-verified 2026-09-13): one Statistics(sizeInBytes=X unit, rowCount=Y) tuple per
    # plan node, larger unit suffixes (GB) alongside smaller ones (B) in the same plan.
    explain_output = (
        "== Optimized Logical Plan ==\n"
        "Aggregate [count(1) AS count(1)#2L], Statistics(sizeInBytes=20.0 B, rowCount=1)\n"
        "+- Relation[ss_store_sk,ss_sold_date_sk] parquet, "
        "Statistics(sizeInBytes=134.6 GB, rowCount=2.88E+9, hints=none)\n"
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(explain_output,)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT COUNT(*) FROM store_sales", warehouse=None)

    executed_sql = mock_cursor.execute.call_args[0][0]
    assert executed_sql == "EXPLAIN COST SELECT COUNT(*) FROM store_sales"
    assert estimate.engine == "databricks"
    assert estimate.accuracy_tier == AccuracyTier.HEURISTIC
    assert estimate.estimated_bytes == int(134.6 * 1024**3)
    assert estimate.estimated_cost_usd is not None


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_handles_missing_statistics(mock_connect):
    # Real, confirmed case: no ANALYZE TABLE run, streaming source, or non-Delta external
    # table with no collected stats.
    explain_output = "== Optimized Logical Plan ==\nRelation[a,b] csv\n"
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(explain_output,)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT * FROM external_csv_table", warehouse=None)

    assert estimate.estimated_bytes is None
    assert estimate.accuracy_tier == AccuracyTier.HEURISTIC
    assert any("no size statistics" in c.lower() for c in estimate.caveats)


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_cost_math_matches_pricing_table(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("Relation[a] parquet, Statistics(sizeInBytes=1.0 B)",)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT 1", warehouse=None, warehouse_size="Small")

    from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU, dbus_per_hour

    expected_cost = round(dbus_per_hour("Small") * SERVERLESS_USD_PER_DBU * (30 / 3600), 6)
    assert estimate.estimated_cost_usd == expected_cost


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_cost_scales_up_for_large_byte_estimate(mock_connect):
    explain_output = "Relation[a] parquet, Statistics(sizeInBytes=50.0 GB, rowCount=1)"
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(explain_output,)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT * FROM huge_table", warehouse=None, warehouse_size="Small")

    from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU, dbus_per_hour

    baseline = 30 / 3600
    expected_cost = round(dbus_per_hour("Small") * SERVERLESS_USD_PER_DBU * baseline * 4, 6)
    assert estimate.estimated_cost_usd == expected_cost


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_closes_connection_on_success(mock_connect):
    explain_output = "Relation[a] parquet, Statistics(sizeInBytes=1.0 B)"
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(explain_output,)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    explain_estimate("SELECT 1", warehouse=None)

    mock_conn.close.assert_called_once()


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_closes_connection_even_on_failure(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = RuntimeError("EXPLAIN COST failed")
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="EXPLAIN COST failed"):
        explain_estimate("SELECT 1", warehouse=None)

    mock_conn.close.assert_called_once()


from cost_guard_mcp.engines.databricks import execute_bounded
from cost_guard_mcp.errors import SanitizedEngineError


def _make_mock_cursor(rows, execute_delay_seconds=0.0, columns=("a",)):
    # Regression fixture: the real databricks-sql-connector row type is a plain tuple
    # with no .keys() method - unlike bigquery.Row, dict(row) fails against it. Modeling
    # rows as tuples (with a matching cursor.description) here, rather than as plain
    # dicts, is what makes this fixture actually catch that bug class instead of masking
    # it - a live run against a real warehouse hit this exact failure.
    mock_cursor = MagicMock()

    def _execute(_sql):
        if execute_delay_seconds:
            time.sleep(execute_delay_seconds)

    mock_cursor.execute.side_effect = _execute
    mock_cursor.fetchall.return_value = rows
    mock_cursor.description = [(col,) for col in columns]
    return mock_cursor


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_wraps_query_with_limit_when_max_rows_set(mock_connect):
    mock_cursor = _make_mock_cursor([(1,), (2,), (3,)])
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    rows, row_count, row_cap_hit = execute_bounded("SELECT * FROM t", warehouse=None, max_rows=2)

    called_sql = mock_cursor.execute.call_args[0][0]
    assert "LIMIT 3" in called_sql  # max_rows + 1
    assert row_count == 2
    assert row_cap_hit is True
    assert rows == [{"a": 1}, {"a": 2}]


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_no_cap_hit_when_fewer_rows_than_max(mock_connect):
    mock_cursor = _make_mock_cursor([(1,)])
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    _rows, row_count, row_cap_hit = execute_bounded("SELECT * FROM t", warehouse=None, max_rows=5)

    assert row_count == 1
    assert row_cap_hit is False
    assert _rows == [{"a": 1}]


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_cancels_and_raises_when_wait_times_out(mock_connect):
    # The plan's original test patched the module-level `_MAX_EXECUTION_WAIT_SECONDS`
    # constant via `execute_bounded.__wrapped__.__globals__["_MAX_EXECUTION_WAIT_SECONDS"]`
    # as a `mock.patch` target string. Confirmed unworkable: `mock.patch`'s target resolver
    # parses dotted paths via getattr chains and cannot resolve a bracketed subscript, so
    # that target raises `AttributeError: ... does not have the attribute
    # '__globals__["_MAX_EXECUTION_WAIT_SECONDS"]'` rather than patching anything. Using the
    # plan's own documented fallback instead: a private `_max_wait_seconds` keyword-only
    # override on `execute_bounded` itself. The observable behavior under test is unchanged
    # from the plan - cancel called, SanitizedEngineError raised, message contains
    # "Query exceeded".
    mock_cursor = _make_mock_cursor([], execute_delay_seconds=2.0)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="Query exceeded"):
        execute_bounded(
            "SELECT * FROM huge_table",
            warehouse=None,
            max_rows=None,
            _max_wait_seconds=0.2,
        )

    mock_cursor.cancel.assert_called_once()


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_strips_trailing_semicolon_before_wrapping(mock_connect):
    mock_cursor = _make_mock_cursor([(1,)])
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    execute_bounded("SELECT * FROM t;", warehouse=None, max_rows=5)

    called_sql = mock_cursor.execute.call_args[0][0]
    assert ";)" not in called_sql
    assert called_sql == "SELECT * FROM (SELECT * FROM t) AS cost_guard_row_cap LIMIT 6"


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_reraises_real_query_error_not_a_timeout(mock_connect):
    # Regression test: the background-thread query-FAILURE path (as opposed to a timeout)
    # had zero test coverage. cur.execute() raising a real error (bad SQL, missing table,
    # permission denied) is the most realistic failure mode for run_query_bounded, and is
    # exactly the kind of concurrency code most prone to a subtle bug (e.g. forgetting to
    # check execution_error, or re-raising the wrong object).
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="TABLE_OR_VIEW_NOT_FOUND"):
        execute_bounded("SELECT * FROM missing_table", warehouse=None, max_rows=None)

    mock_cursor.cancel.assert_not_called()


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_closes_connection_on_success(mock_connect):
    mock_cursor = _make_mock_cursor([(1,)])
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    execute_bounded("SELECT * FROM t", warehouse=None, max_rows=None)

    mock_conn.close.assert_called_once()


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_closes_connection_even_on_failure(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="TABLE_OR_VIEW_NOT_FOUND"):
        execute_bounded("SELECT * FROM missing_table", warehouse=None, max_rows=None)

    mock_conn.close.assert_called_once()
