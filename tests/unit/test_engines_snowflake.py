import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.engines.snowflake import (
    _ASSUMED_RUNTIME_HOURS,
    _LOGIN_TIMEOUT_SECONDS,
    _NETWORK_TIMEOUT_SECONDS,
    _connect,
    check_credentials,
    execute_bounded,
    explain_estimate,
)
from cost_guard_mcp.errors import SanitizedEngineError
from cost_guard_mcp.pricing.snowflake_pricing import credits_per_hour, usd_per_credit
from cost_guard_mcp.types import AccuracyTier


@patch("cost_guard_mcp.engines.snowflake.snowflake.connector.connect")
@patch("cost_guard_mcp.engines.snowflake.load_snowflake_config")
def test_connect_uses_key_pair_when_private_key_path_set(mock_load_config, mock_connect):
    mock_load_config.return_value = MagicMock(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        private_key_path="/tmp/rsa_key.p8",
        private_key_passphrase="pw",
        password=None,
    )

    _connect()

    mock_connect.assert_called_once_with(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        authenticator="SNOWFLAKE_JWT",
        private_key_file="/tmp/rsa_key.p8",
        private_key_file_pwd="pw",
        login_timeout=_LOGIN_TIMEOUT_SECONDS,
        network_timeout=_NETWORK_TIMEOUT_SECONDS,
    )


@patch("cost_guard_mcp.engines.snowflake.snowflake.connector.connect")
@patch("cost_guard_mcp.engines.snowflake.load_snowflake_config")
def test_connect_falls_back_to_password_when_no_key_pair(mock_load_config, mock_connect):
    mock_load_config.return_value = MagicMock(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        private_key_path=None,
        private_key_passphrase=None,
        password="hunter2",
    )

    _connect()

    mock_connect.assert_called_once_with(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        password="hunter2",
        login_timeout=_LOGIN_TIMEOUT_SECONDS,
        network_timeout=_NETWORK_TIMEOUT_SECONDS,
    )


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_pins_warehouse_and_parses_global_stats(mock_connect):
    plan_json = json.dumps(
        {
            "GlobalStats": {
                "partitionsTotal": 10,
                "partitionsAssigned": 4,
                "bytesAssigned": 4_000_000_000,
            }
        }
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT * FROM t", warehouse="COMPUTE_WH", warehouse_size="SMALL")

    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert "USE WAREHOUSE COMPUTE_WH" in executed_sql[0]
    assert "EXPLAIN USING JSON SELECT * FROM t" in executed_sql[1]
    assert estimate.accuracy_tier == AccuracyTier.UPPER_BOUND
    assert estimate.estimated_bytes == 4_000_000_000
    assert estimate.estimated_cost_usd is not None
    assert any("Cortex AI Function" in c for c in estimate.caveats)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_cost_math_matches_credit_rate_times_price(mock_connect):
    # bytesAssigned is informational; cost comes from credits_per_hour * an assumed runtime
    # estimate — verify the formula is wired to the pricing table, not hardcoded.
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate(
        "SELECT 1", warehouse="WH", warehouse_size="XSMALL", edition="standard"
    )

    expected_cost = round(
        credits_per_hour("XSMALL") * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6
    )
    assert estimate.estimated_cost_usd == expected_cost


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_cost_scales_up_for_large_byte_estimate(mock_connect):
    plan_json = json.dumps(
        {
            "GlobalStats": {
                "partitionsTotal": 1000,
                "partitionsAssigned": 1000,
                "bytesAssigned": 50 * 1024**3,
            }
        }
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT * FROM huge_table", warehouse="WH", warehouse_size="XSMALL")

    baseline = 30 / 3600
    expected = round(credits_per_hour("XSMALL") * usd_per_credit("standard") * baseline * 4, 6)
    assert estimate.estimated_cost_usd == expected


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_skips_use_warehouse_when_warehouse_is_none(mock_connect):
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    explain_estimate("SELECT 1", warehouse=None)

    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert len(executed_sql) == 1
    assert not any("USE WAREHOUSE" in stmt for stmt in executed_sql)
    assert "EXPLAIN USING JSON SELECT 1" in executed_sql[0]


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_rejects_invalid_warehouse_name_before_executing_sql(mock_connect):
    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    # explain_estimate is wrapped by @sanitize_exceptions, so the ValueError raised by
    # _validate_warehouse surfaces to callers as SanitizedEngineError, not a bare ValueError.
    with pytest.raises(SanitizedEngineError, match="Invalid warehouse name"):
        explain_estimate("SELECT 1", warehouse="WH1; malicious")

    mock_cursor.execute.assert_not_called()


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_rejects_empty_string_warehouse(mock_connect):
    # Empty string warehouse="" must raise, not silently fallback to current warehouse.
    # This catches upstream bugs like template substitution errors that leave warehouse empty.
    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="Invalid warehouse name"):
        explain_estimate("SELECT 1", warehouse="")

    mock_cursor.execute.assert_not_called()


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_raises_clear_error_when_explain_returns_no_rows(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="EXPLAIN USING JSON returned no rows"):
        explain_estimate("SELECT 1", warehouse=None)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_ok_and_pins_warehouse_when_given(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("COST_GUARD_READER", "COMPUTE_WH", "ABC12345")
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials(warehouse="COMPUTE_WH")

    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert "USE WAREHOUSE COMPUTE_WH" in executed_sql[0]
    assert result.engine == "snowflake"
    assert result.ok is True
    assert "COST_GUARD_READER" in result.detail
    assert "COMPUTE_WH" in result.detail
    assert "ABC12345" in result.detail


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_skips_use_warehouse_when_none(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("COST_GUARD_READER", None, "ABC12345")
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    check_credentials(warehouse=None)

    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert len(executed_sql) == 1
    assert not any("USE WAREHOUSE" in stmt for stmt in executed_sql)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_reports_connection_failure_as_data_not_a_raise(mock_connect):
    mock_connect.side_effect = SanitizedEngineError("snowflake client call failed: bad account")

    result = check_credentials()

    assert result.engine == "snowflake"
    assert result.ok is False
    assert "bad account" in result.detail


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_redacts_secrets_in_failure_detail(mock_connect):
    body = base64.b64decode(b"YWJjMTIzc2VjcmV0").decode()
    prefix = "-----BEGIN " + "PRIVATE KEY" + "-----"
    mock_connect.side_effect = Exception("auth error, private_key=" + prefix + body)

    result = check_credentials()

    assert result.ok is False
    assert body not in result.detail
    assert "REDACTED" in result.detail


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_reports_no_rows_as_failure_not_a_raise(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials()

    assert result.ok is False
    assert "no rows" in result.detail


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_rejects_invalid_warehouse_name_as_failure_not_a_raise(mock_connect):
    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials(warehouse="WH1; malicious")

    assert result.ok is False
    assert "Invalid warehouse name" in result.detail
    mock_cursor.execute.assert_not_called()


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_polls_until_done_then_fetches_results(mock_connect, _mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_cursor.fetchall.return_value = [{"a": 1}]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.side_effect = [True, False]
    mock_connect.return_value = mock_conn

    rows, row_count, row_cap_hit = execute_bounded("SELECT 1", warehouse=None, max_rows=None)

    mock_conn.get_query_status_throw_if_error.assert_called_once_with(mock_cursor.sfqid)
    mock_cursor.get_results_from_sfqid.assert_called_once_with(mock_cursor.sfqid)
    assert rows == [{"a": 1}]
    assert row_count == 1
    assert row_cap_hit is False


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_pins_warehouse_before_execute_async(mock_connect, _mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = False
    mock_connect.return_value = mock_conn

    execute_bounded("SELECT 1", warehouse="COMPUTE_WH", max_rows=None)

    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert "USE WAREHOUSE COMPUTE_WH" in executed_sql[0]


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_still_applies_row_cap_after_async_wait(mock_connect, _mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_cursor.fetchall.return_value = [{"a": 1}, {"a": 2}, {"a": 3}]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = False
    mock_connect.return_value = mock_conn

    rows, row_count, row_cap_hit = execute_bounded("SELECT * FROM t", warehouse=None, max_rows=2)

    called_sql = mock_cursor.execute_async.call_args[0][0]
    assert "LIMIT 3" in called_sql  # max_rows + 1
    assert row_count == 2
    assert row_cap_hit is True
    assert rows == [{"a": 1}, {"a": 2}]


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_cancels_and_raises_when_wait_times_out(mock_connect, mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = True  # never finishes
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="Query exceeded 120s and was cancelled"):
        execute_bounded("SELECT * FROM huge_table", warehouse=None, max_rows=None)

    mock_sleep.assert_called()
    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert any("SYSTEM$CANCEL_QUERY" in stmt for stmt in executed_sql)
    mock_conn.get_query_status_throw_if_error.assert_not_called()


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_raise_survives_a_failed_cancel_attempt(mock_connect, _mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = True
    mock_connect.return_value = mock_conn
    mock_cursor.execute.side_effect = Exception("cancel also failed")

    with pytest.raises(SanitizedEngineError, match="Query exceeded 120s and was cancelled"):
        execute_bounded("SELECT * FROM huge_table", warehouse=None, max_rows=None)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_raises_when_execute_async_returns_no_query_id(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = None
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="did not return a query id"):
        execute_bounded("SELECT 1", warehouse=None, max_rows=None)


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_strips_trailing_semicolon_before_wrapping(mock_connect, _mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = False
    mock_connect.return_value = mock_conn

    execute_bounded("SELECT * FROM t;", warehouse=None, max_rows=5)

    called_sql = mock_cursor.execute_async.call_args[0][0]
    assert ";)" not in called_sql
    assert called_sql == "SELECT * FROM (SELECT * FROM t) AS cost_guard_row_cap LIMIT 6"
