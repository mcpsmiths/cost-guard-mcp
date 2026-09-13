import json
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.engines.snowflake import _ASSUMED_RUNTIME_HOURS, _connect, explain_estimate
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
        account="abc123", user="svc_user", role="COST_GUARD_READER", password="hunter2"
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
