import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.engines.snowflake import (
    _ASSUMED_RUNTIME_HOURS,
    _GENERATION_LOOKUP_TIMEOUT_SECONDS,
    _HISTORY_LOOKUP_TIMEOUT_SECONDS,
    _LOGIN_TIMEOUT_SECONDS,
    _MAX_EXECUTION_WAIT_SECONDS,
    _NETWORK_TIMEOUT_SECONDS,
    _SESSION_PARAMETER_PROBE_TIMEOUT_SECONDS,
    _SINGLE_STATEMENT_COUNT,
    _apply_statement_timeout,
    _connect,
    _lookup_historical_runtime,
    _lookup_warehouse_facts,
    _normalize_sql_for_matching,
    _size_to_pricing_key,
    _WarehouseFacts,
    check_credentials,
    execute_bounded,
    explain_estimate,
)
from cost_guard_mcp.errors import SanitizedEngineError
from cost_guard_mcp.pricing.runtime_scaling import scale_runtime_hours
from cost_guard_mcp.pricing.snowflake_pricing import credits_per_hour, usd_per_credit
from cost_guard_mcp.types import AccuracyTier, HistoricalRuntimeSignal


@pytest.fixture(autouse=True)
def _snowflake_config_for_every_test(snowflake_config):
    """explain_estimate reads SnowflakeConfig.edition before it connects (see conftest)."""
    return snowflake_config


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
        secondary_roles="NONE",
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
        secondary_roles="NONE",
        session_parameters={"MULTI_STATEMENT_COUNT": 1},
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
        secondary_roles="NONE",
    )

    _connect()

    mock_connect.assert_called_once_with(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        password="hunter2",
        login_timeout=_LOGIN_TIMEOUT_SECONDS,
        network_timeout=_NETWORK_TIMEOUT_SECONDS,
        secondary_roles="NONE",
        session_parameters={"MULTI_STATEMENT_COUNT": 1},
    )


@patch("cost_guard_mcp.engines.snowflake.snowflake.connector.connect")
@patch("cost_guard_mcp.engines.snowflake.load_snowflake_config")
def test_connect_passes_secondary_roles_all_opt_out_through_unchanged(
    mock_load_config, mock_connect
):
    # SNOWFLAKE_SECONDARY_ROLES=ALL is the documented opt-out for deployments that reached
    # objects only through a secondary role; the engine must forward exactly what config
    # validated, never re-derive or default it here.
    mock_load_config.return_value = MagicMock(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        private_key_path="/tmp/rsa_key.p8",
        private_key_passphrase=None,
        password=None,
        secondary_roles="ALL",
    )

    _connect()

    assert mock_connect.call_args.kwargs["secondary_roles"] == "ALL"


_SHOW_PARAMETERS_SQL = "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION"


def _connection_reporting_statement_timeout(effective_value):
    """A mocked connection whose SHOW PARAMETERS probe returns `effective_value` in the
    documented (key, value, default, level, description, type) row shape; None means the
    probe returned no row at all."""
    mock_cursor = MagicMock()
    if effective_value is None:
        mock_cursor.fetchone.return_value = None
    else:
        mock_cursor.fetchone.return_value = (
            "STATEMENT_TIMEOUT_IN_SECONDS",
            effective_value,
            "172800",
            "USER",
            "Timeout in seconds for statements",
            "NUMBER",
        )
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_conn, mock_cursor


def _statements(mock_cursor):
    return [call.args[0] for call in mock_cursor.execute.call_args_list]


def test_session_parameter_probe_timeout_is_short_and_bounded():
    assert 0 < _SESSION_PARAMETER_PROBE_TIMEOUT_SECONDS <= _HISTORY_LOOKUP_TIMEOUT_SECONDS


def test_apply_statement_timeout_keeps_a_stricter_operator_policy():
    # Plan-review amendment A8: a SESSION-level STATEMENT_TIMEOUT_IN_SECONDS OVERRIDES the
    # account- and user-level values (only the warehouse level combines lowest-wins), so a
    # blind ALTER SESSION to 60 would RAISE an operator's stricter 30 s user policy.
    # Read-then-only-lower never does: 30 <= 60 -> leave it alone.
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("30")

    _apply_statement_timeout(mock_conn, 60)

    assert _statements(mock_cursor) == [_SHOW_PARAMETERS_SQL]
    assert mock_cursor.execute.call_args_list[0].kwargs == {
        "timeout": _SESSION_PARAMETER_PROBE_TIMEOUT_SECONDS
    }
    mock_conn.close.assert_not_called()


def test_apply_statement_timeout_keeps_a_value_equal_to_the_target():
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("60")

    _apply_statement_timeout(mock_conn, 60)

    assert _statements(mock_cursor) == [_SHOW_PARAMETERS_SQL]


def test_apply_statement_timeout_lowers_the_snowflake_default():
    # 172800 (2 days) is Snowflake's shipped default - far above anything a pre-flight tool
    # should let a query run for once the client-side watchdog has died with the process.
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("172800")

    _apply_statement_timeout(mock_conn, 60)

    assert _statements(mock_cursor) == [
        _SHOW_PARAMETERS_SQL,
        "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 60",
    ]
    assert mock_cursor.execute.call_args_list[1].kwargs == {
        "timeout": _SESSION_PARAMETER_PROBE_TIMEOUT_SECONDS
    }


def test_apply_statement_timeout_lowers_a_value_just_above_the_target():
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("61")

    _apply_statement_timeout(mock_conn, 60)

    assert _statements(mock_cursor)[-1] == "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 60"


def test_apply_statement_timeout_treats_zero_as_unbounded_and_lowers_it():
    # 0 means "no timeout" in Snowflake - the least bounded possible value, not "stricter".
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("0")

    _apply_statement_timeout(mock_conn, 60)

    assert _statements(mock_cursor)[-1] == "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 60"


@pytest.mark.parametrize("unparseable", ["abc", "", None, "  ", "12abc"])
def test_apply_statement_timeout_fails_closed_to_the_target_when_value_is_unparseable(
    unparseable,
):
    # Unknown row shape -> fail closed to the project target rather than trusting nothing.
    # (The live integration run after merge confirms the 'value' column parses as an int.)
    mock_conn, mock_cursor = _connection_reporting_statement_timeout(unparseable)

    _apply_statement_timeout(mock_conn, 60)

    assert _statements(mock_cursor)[-1] == "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 60"


def test_apply_statement_timeout_fails_closed_when_probe_returns_no_row():
    mock_conn, mock_cursor = _connection_reporting_statement_timeout(None)

    _apply_statement_timeout(mock_conn, 60)

    assert _statements(mock_cursor)[-1] == "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 60"


def test_apply_statement_timeout_accepts_an_integer_typed_value():
    mock_conn, mock_cursor = _connection_reporting_statement_timeout(45)

    _apply_statement_timeout(mock_conn, 60)

    assert _statements(mock_cursor) == [_SHOW_PARAMETERS_SQL]


def test_apply_statement_timeout_closes_connection_and_reraises_on_probe_failure():
    # A half-configured session must not be handed back to the caller: close it and let the
    # @sanitize_exceptions decorator on _connect turn the failure into a SanitizedEngineError.
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("172800")
    mock_cursor.execute.side_effect = RuntimeError("SHOW PARAMETERS failed")

    with pytest.raises(RuntimeError, match="SHOW PARAMETERS failed"):
        _apply_statement_timeout(mock_conn, 60)

    mock_conn.close.assert_called_once()


def test_apply_statement_timeout_reraises_even_when_close_also_fails():
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("172800")
    mock_cursor.execute.side_effect = RuntimeError("SHOW PARAMETERS failed")
    mock_conn.close.side_effect = RuntimeError("close also failed")

    with pytest.raises(RuntimeError, match="SHOW PARAMETERS failed"):
        _apply_statement_timeout(mock_conn, 60)


@patch("cost_guard_mcp.engines.snowflake.snowflake.connector.connect")
@patch("cost_guard_mcp.engines.snowflake.load_snowflake_config")
def test_connect_defaults_statement_timeout_target_to_the_network_timeout(
    mock_load_config, mock_connect
):
    # Read-only paths (explain_estimate, check_credentials) use the default 60 s target; the
    # session-level deadline then survives even if this process dies mid-call.
    mock_load_config.return_value = MagicMock(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        private_key_path=None,
        private_key_passphrase=None,
        password="hunter2",
        secondary_roles="NONE",
    )
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("172800")
    mock_connect.return_value = mock_conn

    returned = _connect()

    assert returned is mock_conn
    assert _statements(mock_cursor) == [
        _SHOW_PARAMETERS_SQL,
        f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {_NETWORK_TIMEOUT_SECONDS}",
    ]


@patch("cost_guard_mcp.engines.snowflake.snowflake.connector.connect")
@patch("cost_guard_mcp.engines.snowflake.load_snowflake_config")
def test_connect_honours_an_explicit_statement_timeout_target(mock_load_config, mock_connect):
    mock_load_config.return_value = MagicMock(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        private_key_path="/tmp/rsa_key.p8",
        private_key_passphrase=None,
        password=None,
        secondary_roles="NONE",
    )
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("172800")
    mock_connect.return_value = mock_conn

    _connect(statement_timeout_seconds=_MAX_EXECUTION_WAIT_SECONDS)

    assert _statements(mock_cursor)[-1] == (
        f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {_MAX_EXECUTION_WAIT_SECONDS}"
    )


@patch("cost_guard_mcp.engines.snowflake.snowflake.connector.connect")
@patch("cost_guard_mcp.engines.snowflake.load_snowflake_config")
def test_connect_sanitizes_and_closes_when_the_timeout_probe_fails(mock_load_config, mock_connect):
    mock_load_config.return_value = MagicMock(
        account="abc123",
        user="svc_user",
        role="COST_GUARD_READER",
        private_key_path=None,
        private_key_passphrase=None,
        password="hunter2",
        secondary_roles="NONE",
    )
    mock_conn, mock_cursor = _connection_reporting_statement_timeout("172800")
    mock_cursor.execute.side_effect = RuntimeError("probe failed, password=hunter2")
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="probe failed") as excinfo:
        _connect()

    assert "hunter2" not in str(excinfo.value)
    mock_conn.close.assert_called_once()


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

    # Read-only path: the default (60 s) session statement-timeout target, never the
    # execute path's 120 s.
    mock_connect.assert_called_once_with()
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
    # Three statements: EXPLAIN, then SELECT CURRENT_WAREHOUSE() (the warehouse-facts lookup
    # resolves the session's active warehouse when none is pinned - this mock answers it with
    # the plan text, which fails the identifier allowlist, so the lookup degrades to None),
    # then the warehouse-history calibration lookup. None of them issues USE WAREHOUSE when
    # warehouse=None - that's the substantive behaviour this test guards.
    assert len(executed_sql) == 3
    assert not any("USE WAREHOUSE" in stmt for stmt in executed_sql)
    assert "EXPLAIN USING JSON SELECT 1" in executed_sql[0]
    assert "SELECT CURRENT_WAREHOUSE()" in executed_sql[1]


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


def test_single_statement_count_constant_is_one():
    assert _SINGLE_STATEMENT_COUNT == 1


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_pins_statement_count_to_one_on_explain(mock_connect):
    # Security audit snowflake-execute-unpinned-multi-statement-count: EXPLAIN USING JSON
    # carries model-supplied text, and without num_statements the connector sends no
    # MULTI_STATEMENT_COUNT, so whether ';'-separated trailing text executes was decided by
    # the operator's account/user parameter. num_statements=1 makes Snowflake itself reject
    # anything but exactly one statement, regardless of that parameter.
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    explain_estimate("SELECT 1; CREATE TABLE cg_probe(x INT)", warehouse=None)

    explain_calls = [
        call
        for call in mock_cursor.execute.call_args_list
        if call.args[0].startswith("EXPLAIN USING JSON")
    ]
    assert len(explain_calls) == 1
    assert explain_calls[0].kwargs["num_statements"] == 1


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_statement_count_rejection_propagates_and_closes_connection(
    mock_connect,
):
    # Simulates Snowflake's server-side rejection under MULTI_STATEMENT_COUNT=1. Nothing is
    # fetched or estimated, the connection is still closed, and the caller sees a
    # SanitizedEngineError (-> ToolError) rather than a normal CostEstimate.
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = Exception(
        "Actual statement count 2 did not match the desired statement count 1."
    )
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="did not match the desired statement count"):
        explain_estimate("SELECT 1; CREATE TABLE cg_probe(x INT)", warehouse=None)

    mock_cursor.fetchone.assert_not_called()
    mock_conn.close.assert_called_once()


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
def test_explain_estimate_closes_connection_on_success(mock_connect):
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    explain_estimate("SELECT 1", warehouse=None)

    mock_conn.close.assert_called_once()


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_closes_connection_even_on_failure(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="EXPLAIN USING JSON returned no rows"):
        explain_estimate("SELECT 1", warehouse=None)

    mock_conn.close.assert_called_once()


def test_normalize_sql_collapses_whitespace_but_not_case():
    assert _normalize_sql_for_matching("SELECT   1\n  FROM t") == "SELECT 1 FROM t"
    assert _normalize_sql_for_matching("select 1 from t") != _normalize_sql_for_matching(
        "SELECT 1 FROM t"
    )


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_finds_single_real_match(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("SELECT 1 FROM t", 4000, 1024),  # real execution: 4000ms, bytes_scanned > 0
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    signal = _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    assert signal == HistoricalRuntimeSignal(avg_runtime_hours=4000 / 1000 / 3600, sample_count=1)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_averages_multiple_matches(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("SELECT 1 FROM t", 4000, 1024),
        ("SELECT   1 FROM t", 6000, 2048),  # whitespace-differing, still matches
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    signal = _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    assert signal.sample_count == 2
    assert signal.avg_runtime_hours == (5000 / 1000 / 3600)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_excludes_cache_hits(mock_connect):
    # The single most important test in this feature (per the design spec) - a cache hit
    # (bytes_scanned == 0) must never be averaged in, or calibration silently corrupts
    # itself toward underestimating future runtime.
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("SELECT 1 FROM t", 4000, 1024),  # real execution
        ("SELECT 1 FROM t", 5, 0),  # cache hit - near-instant, zero bytes scanned
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    signal = _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    assert signal.sample_count == 1  # only the real execution counted
    assert signal.avg_runtime_hours == 4000 / 1000 / 3600


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_returns_none_when_all_matches_are_cache_hits(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("SELECT 1 FROM t", 5, 0)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    assert _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t") is None


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_returns_none_when_no_text_match(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("SELECT * FROM other_table", 4000, 1024)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    assert _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t") is None


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_returns_none_on_exception(mock_connect):
    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = RuntimeError("permission denied")
    mock_connect.return_value = mock_conn

    assert _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t") is None


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_treats_none_query_text_as_non_match(mock_connect):
    # Regression test: a real QUERY_HISTORY row with QUERY_TEXT=None previously crashed
    # with AttributeError ('NoneType' object has no attribute 'split') from
    # _normalize_sql_for_matching - it must be treated as a non-match instead, and must not
    # stop the good row below it from being counted.
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        (None, 4000, 1024),
        ("SELECT 1 FROM t", 4000, 1024),
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    signal = _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    assert signal.sample_count == 1
    assert signal.avg_runtime_hours == 4000 / 1000 / 3600


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_treats_none_bytes_scanned_as_non_match(mock_connect):
    # Regression test: a real QUERY_HISTORY row with BYTES_SCANNED=None previously crashed
    # with TypeError ('>' not supported between instances of 'NoneType' and 'int') - it must
    # be treated as a non-match instead, not as a real (or cache-hit) execution.
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("SELECT 1 FROM t", 4000, None),
        ("SELECT 1 FROM t", 5000, 2048),
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    signal = _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    assert signal.sample_count == 1
    assert signal.avg_runtime_hours == 5000 / 1000 / 3600


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_bounds_the_query_with_a_timeout(mock_connect):
    # The design spec requires the lookup be bounded by _HISTORY_LOOKUP_TIMEOUT_SECONDS -
    # verify it's actually wired into cur.execute()'s own `timeout` kwarg (the connector's
    # client-side cancel-after-N-seconds mechanism), not just documented in a comment.
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    _, kwargs = mock_cursor.execute.call_args
    assert kwargs.get("timeout") == _HISTORY_LOOKUP_TIMEOUT_SECONDS


# --- _lookup_warehouse_facts (observed size/type/generation/state) ---------------------------


def _facts(**overrides):
    base = {
        "name": "COMPUTE_WH",
        "generation": "2",
        "cloud_provider": "AWS",
        "size_key": "XSMALL",
        "size_text": "X-Small",
        "warehouse_type": "STANDARD",
        "state": "STARTED",
        "auto_resume": "true",
    }
    return _WarehouseFacts(**{**base, **overrides})


def test_lookup_warehouse_facts_reads_the_show_warehouses_row_directly(snowflake_fakes):
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small", generation="2")],
        region="AWS_US_WEST_2",
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH")

    assert facts == _WarehouseFacts(
        name="COMPUTE_WH",
        generation="2",
        cloud_provider="AWS",
        size_key="XSMALL",
        size_text="X-Small",
        warehouse_type="STANDARD",
        state="STARTED",
        auto_resume="true",
    )
    # Two metadata statements only: the SHOW row is read through fetchall()/description,
    # never through a RESULT_SCAN(LAST_QUERY_ID(-1)) SELECT (one statement fewer, same data).
    assert cursor.executed == ["SHOW WAREHOUSES LIKE 'COMPUTE_WH'", "SELECT CURRENT_REGION()"]
    assert not any("RESULT_SCAN" in stmt for stmt in cursor.executed)


def test_lookup_warehouse_facts_bounds_every_statement_with_a_timeout(snowflake_fakes):
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small")],
        current_warehouse="COMPUTE_WH",
    )

    _lookup_warehouse_facts(snowflake_fakes.connection(cursor), None)

    assert len(cursor.timeouts) == 3
    assert all(timeout == _GENERATION_LOOKUP_TIMEOUT_SECONDS for timeout in cursor.timeouts)


def test_lookup_warehouse_facts_resolves_current_warehouse_when_none_given(snowflake_fakes):
    # Replaces the old "warehouse=None -> None" short-circuit: the executing warehouse is
    # whatever the session has active, and SELECT CURRENT_WAREHOUSE() names it.
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("ANALYTICS_XL", "4X-Large")],
        current_warehouse="ANALYTICS_XL",
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), None)

    assert facts is not None
    assert facts.name == "ANALYTICS_XL"
    assert facts.size_key == "X4LARGE"
    assert cursor.executed == [
        "SELECT CURRENT_WAREHOUSE()",
        "SHOW WAREHOUSES LIKE 'ANALYTICS_XL'",
        "SELECT CURRENT_REGION()",
    ]


def test_lookup_warehouse_facts_returns_none_when_session_has_no_current_warehouse(
    snowflake_fakes,
):
    cursor = snowflake_fakes.cursor(show_rows=[], current_warehouse=None)

    assert _lookup_warehouse_facts(snowflake_fakes.connection(cursor), None) is None
    assert not any(stmt.startswith("SHOW WAREHOUSES") for stmt in cursor.executed)


def test_lookup_warehouse_facts_returns_none_when_current_warehouse_fails_the_allowlist(
    snowflake_fakes,
):
    # The resolved name is interpolated into SHOW WAREHOUSES LIKE '<name>' exactly like a
    # caller-supplied one, so it gets the same DECISIONS.md #6 validation first.
    cursor = snowflake_fakes.cursor(show_rows=[], current_warehouse="WH'; DROP WAREHOUSE X")

    assert _lookup_warehouse_facts(snowflake_fakes.connection(cursor), None) is None
    assert not any(stmt.startswith("SHOW WAREHOUSES") for stmt in cursor.executed)


def test_lookup_warehouse_facts_matches_the_row_name_case_insensitively(snowflake_fakes):
    # Amendment A3: unquoted Snowflake identifiers are stored upper-cased and the allowlist
    # forbids quoted names, so a caller passing 'compute_wh' must still bind to the
    # 'COMPUTE_WH' row - otherwise the model controls whether the observed size applies.
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "4X-Large")],
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "compute_wh")

    assert facts is not None
    assert facts.name == "COMPUTE_WH"
    assert facts.size_key == "X4LARGE"
    assert cursor.executed[0] == "SHOW WAREHOUSES LIKE 'compute_wh'"


def test_lookup_warehouse_facts_returns_none_when_names_genuinely_differ(snowflake_fakes):
    # Negative control for the case-insensitive match: LIKE's '_' wildcard makes Snowflake
    # return MYAWH for the pattern MY_WH; it is a different warehouse and must not bind.
    cursor = snowflake_fakes.cursor(show_rows=[snowflake_fakes.show_row("MYAWH", "6X-Large")])

    assert _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "MY_WH") is None


def test_lookup_warehouse_facts_ignores_like_wildcard_sibling_rows(snowflake_fakes):
    cursor = snowflake_fakes.cursor(
        show_rows=[
            snowflake_fakes.show_row("COMPUTEXWH", "6X-Large"),
            snowflake_fakes.show_row("COMPUTE_WH", "Small"),
        ]
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH")

    assert facts is not None
    assert facts.name == "COMPUTE_WH"
    assert facts.size_key == "SMALL"


@pytest.mark.parametrize(
    ("size_text", "expected"),
    [
        ("X-Small", "XSMALL"),
        ("Small", "SMALL"),
        ("Medium", "MEDIUM"),
        ("Large", "LARGE"),
        ("X-Large", "XLARGE"),
        ("2X-Large", "X2LARGE"),
        ("3X-Large", "X3LARGE"),
        ("4X-Large", "X4LARGE"),
        ("5X-Large", "X5LARGE"),
        ("6X-Large", "X6LARGE"),
        (" x-small ", "XSMALL"),
        ("XSMALL", "XSMALL"),
        ("7X-Large", None),
        ("", None),
        (None, None),
        (4, None),
    ],
)
def test_size_to_pricing_key_normalises_show_warehouses_size_text(size_text, expected):
    assert _size_to_pricing_key(size_text) == expected


def test_lookup_warehouse_facts_keeps_unmodelled_type_and_size_as_facts(snowflake_fakes):
    # A Snowpark-optimized warehouse or an unrecognised size is still an observation; the
    # fail-closed decision belongs to explain_estimate (tested below), not to the lookup.
    cursor = snowflake_fakes.cursor(
        show_rows=[
            snowflake_fakes.show_row("SNOWPARK_WH", "7X-Large", warehouse_type="SNOWPARK-OPTIMIZED")
        ]
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "SNOWPARK_WH")

    assert facts is not None
    assert facts.warehouse_type == "SNOWPARK-OPTIMIZED"
    assert facts.size_key is None


def test_lookup_warehouse_facts_reports_an_unrecognised_generation_as_none(snowflake_fakes):
    # A future/unexpected generation string (e.g. "3") must not be treated as Gen1/Gen2 -
    # but it must not throw away the observed size either.
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "4X-Large", generation="3")]
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH")

    assert facts is not None
    assert facts.generation is None
    assert facts.size_key == "X4LARGE"


def test_lookup_warehouse_facts_accepts_an_integer_generation_column(snowflake_fakes):
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "Small", generation=1)]
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH")

    assert facts is not None
    assert facts.generation == "1"


def test_lookup_warehouse_facts_handles_region_group_prefixed_region(snowflake_fakes):
    # Accounts whose organization spans multiple region groups get "<group>.<CLOUD>_<REGION>"
    # (e.g. "PUBLIC.AWS_US_WEST_2") from CURRENT_REGION() instead of the plain form - both
    # shapes are documented examples in Snowflake's own CURRENT_REGION() reference.
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "Small")], region="PUBLIC.AWS_US_WEST_2"
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH")

    assert facts is not None
    assert facts.cloud_provider == "AWS"


@pytest.mark.parametrize("region", ["ORACLE_US_EAST", None])
def test_lookup_warehouse_facts_reports_an_unusable_region_as_no_cloud_provider(
    snowflake_fakes, region
):
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "Small")], region=region
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH")

    assert facts is not None
    assert facts.cloud_provider is None
    assert facts.size_key == "SMALL"


def test_lookup_warehouse_facts_treats_non_string_columns_as_unknown(snowflake_fakes):
    # A row whose size/type/state/auto_resume are NULL (or a future type change) and a NULL
    # CURRENT_REGION() are still an observation of that warehouse: the unknown fields become
    # None and explain_estimate decides what to do with them.
    cursor = snowflake_fakes.cursor(
        show_rows=[("COMPUTE_WH", None, None, None, None, "2")], region=(None,)
    )

    facts = _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH")

    assert facts is not None
    assert facts.generation == "2"
    assert facts.size_key is None
    assert facts.size_text is None
    assert facts.warehouse_type is None
    assert facts.state is None
    assert facts.auto_resume is None
    assert facts.cloud_provider is None


def test_lookup_warehouse_facts_returns_none_for_invalid_warehouse_name():
    mock_conn = MagicMock()

    assert _lookup_warehouse_facts(mock_conn, "WH1; malicious") is None
    mock_conn.cursor.assert_not_called()


def test_lookup_warehouse_facts_returns_none_on_exception():
    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = RuntimeError("permission denied")

    assert _lookup_warehouse_facts(mock_conn, "COMPUTE_WH") is None


def test_lookup_warehouse_facts_returns_none_when_show_warehouses_fails(snowflake_fakes):
    cursor = snowflake_fakes.cursor(
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "Small")], fail_on=("SHOW WAREHOUSES",)
    )

    assert _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH") is None


def test_lookup_warehouse_facts_returns_none_when_a_row_has_too_few_columns(snowflake_fakes):
    cursor = snowflake_fakes.cursor(show_rows=[("COMPUTE_WH", "STARTED")])

    assert _lookup_warehouse_facts(snowflake_fakes.connection(cursor), "COMPUTE_WH") is None


# --- explain_estimate: the cap-gate rate is bound to the observed warehouse -----------------


def _estimate_with(
    snowflake_fakes,
    *,
    show_rows,
    region="AWS_US_EAST_1",
    bytes_assigned=1000,
    warehouse="COMPUTE_WH",
    current_warehouse=None,
    history_rows=(),
    fail_on=(),
    **kwargs,
):
    cursor = snowflake_fakes.cursor(
        plan={
            "GlobalStats": {
                "partitionsTotal": 10,
                "partitionsAssigned": 4,
                "bytesAssigned": bytes_assigned,
            }
        },
        show_rows=show_rows,
        region=region,
        current_warehouse=current_warehouse,
        history_rows=history_rows,
        fail_on=fail_on,
    )
    with patch(
        "cost_guard_mcp.engines.snowflake._connect",
        return_value=snowflake_fakes.connection(cursor),
    ):
        estimate = explain_estimate("SELECT 1", warehouse=warehouse, **kwargs)
    return estimate, cursor


def test_explain_estimate_prices_at_the_observed_size_when_warehouse_size_is_omitted(
    snowflake_fakes,
):
    # The audit's reproduction: a Gen2/AWS 4X-Large STANDARD warehouse, warehouse_size
    # omitted. The old code priced this at XSMALL (1.35 credits/h); the pinned warehouse
    # bills 172.8 credits/h, so the cap gate compared a figure 128x too low.
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("ANALYTICS_XL", "4X-Large", generation="2")],
        region="AWS_US_EAST_1",
        bytes_assigned=2_500_000,
        warehouse="ANALYTICS_XL",
    )

    expected = round(
        172.8 * 2.0 * scale_runtime_hours(2_500_000, baseline_hours=_ASSUMED_RUNTIME_HOURS), 6
    )
    assert estimate.estimated_cost_usd == expected
    assert estimate.accuracy_tier == AccuracyTier.UPPER_BOUND
    assert any("understated" in c and "4X-Large" in c for c in estimate.caveats)


def test_explain_estimate_lets_the_caller_raise_but_never_lower_the_size(snowflake_fakes):
    # Asserted X4LARGE on an observed Small Gen1 warehouse: the larger assertion wins (the
    # caller may only raise the figure) and no understatement caveat is added.
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "Small", generation="1")],
        warehouse_size="X4LARGE",
    )

    expected = round(
        credits_per_hour("X4LARGE", generation="1", cloud_provider="AWS")
        * usd_per_credit("standard")
        * _ASSUMED_RUNTIME_HOURS,
        6,
    )
    assert estimate.estimated_cost_usd == expected
    assert not any("understated" in c for c in estimate.caveats)


def test_explain_estimate_resolves_current_warehouse_when_warehouse_is_none(snowflake_fakes):
    estimate, cursor = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("ANALYTICS_XL", "4X-Large", generation="2")],
        warehouse=None,
        current_warehouse="ANALYTICS_XL",
    )

    expected = round(172.8 * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6)
    assert estimate.estimated_cost_usd == expected
    assert not any("USE WAREHOUSE" in stmt for stmt in cursor.executed)
    assert cursor.executed.index("SELECT CURRENT_WAREHOUSE()") < cursor.executed.index(
        "SHOW WAREHOUSES LIKE 'ANALYTICS_XL'"
    )


def test_explain_estimate_applies_the_observed_size_for_a_lowercase_warehouse_argument(
    snowflake_fakes,
):
    # Amendment A3, end to end: 'compute_wh' binds to the COMPUTE_WH row, so the observed
    # 4X-Large size applies and a 0.10 USD cap would refuse (see test_tools_run_query_bounded).
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "4X-Large", generation="2")],
        warehouse="compute_wh",
    )

    assert estimate.estimated_cost_usd == round(
        172.8 * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6
    )
    assert estimate.estimated_cost_usd > 0.10


def test_explain_estimate_returns_no_dollar_figure_for_a_non_standard_warehouse_type(
    snowflake_fakes,
):
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[
            snowflake_fakes.show_row("SNOWPARK_WH", "Medium", warehouse_type="SNOWPARK-OPTIMIZED")
        ],
        warehouse="SNOWPARK_WH",
    )

    assert estimate.estimated_cost_usd is None
    assert estimate.estimated_bytes == 1000
    assert any("SNOWPARK-OPTIMIZED" in c and "run_query_bounded" in c for c in estimate.caveats)


def test_explain_estimate_returns_no_dollar_figure_for_unrecognised_size_text(snowflake_fakes):
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "7X-Large")],
    )

    assert estimate.estimated_cost_usd is None
    assert any("7X-Large" in c for c in estimate.caveats)


def test_explain_estimate_keeps_the_asserted_gen1_rate_when_the_lookup_fails(snowflake_fakes):
    # Design decision (recommended default): roles that cannot run SHOW WAREHOUSES keep
    # working at the caller-asserted/default rate with the existing explicit caveat.
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "4X-Large")],
        fail_on=("SHOW WAREHOUSES",),
        warehouse_size="MEDIUM",
    )

    expected = round(
        credits_per_hour("MEDIUM") * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6
    )
    assert estimate.estimated_cost_usd == expected
    assert any("could not be determined" in c for c in estimate.caveats)


@patch("cost_guard_mcp.engines.snowflake._lookup_historical_runtime")
@patch("cost_guard_mcp.engines.snowflake._lookup_warehouse_facts")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_uses_gen2_rate_when_generation_detected(
    mock_connect, mock_lookup_facts, mock_lookup_historical
):
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    mock_lookup_facts.return_value = _facts(generation="2", cloud_provider="AWS", size_key="MEDIUM")
    mock_lookup_historical.return_value = None

    estimate = explain_estimate("SELECT 1", warehouse="WH", warehouse_size="MEDIUM")

    expected_rate = credits_per_hour("MEDIUM", generation="2", cloud_provider="AWS")
    expected_cost = round(expected_rate * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6)
    assert estimate.estimated_cost_usd == expected_cost
    assert any("Gen2 on AWS" in c for c in estimate.caveats)
    assert not any("could not be determined" in c for c in estimate.caveats)


@patch("cost_guard_mcp.engines.snowflake._lookup_historical_runtime")
@patch("cost_guard_mcp.engines.snowflake._lookup_warehouse_facts")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_uses_gen1_rate_when_generation_detected_as_gen1(
    mock_connect, mock_lookup_facts, mock_lookup_historical
):
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    mock_lookup_facts.return_value = _facts(
        generation="1", cloud_provider="AZURE", size_key="MEDIUM"
    )
    mock_lookup_historical.return_value = None

    estimate = explain_estimate("SELECT 1", warehouse="WH", warehouse_size="MEDIUM")

    expected_rate = credits_per_hour("MEDIUM", generation="1", cloud_provider="AZURE")
    expected_cost = round(expected_rate * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6)
    assert estimate.estimated_cost_usd == expected_cost
    assert any("Gen1 on AZURE" in c for c in estimate.caveats)


@patch("cost_guard_mcp.engines.snowflake._lookup_historical_runtime")
@patch("cost_guard_mcp.engines.snowflake._lookup_warehouse_facts")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_still_applies_the_observed_size_when_generation_is_unknown(
    mock_connect, mock_lookup_facts, mock_lookup_historical
):
    # Generation undeterminable keeps the Gen1 fallback (design decision), but the size axis
    # is a separate observation and must still win over an understated assertion.
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    mock_lookup_facts.return_value = _facts(
        generation=None, cloud_provider=None, size_key="X4LARGE"
    )
    mock_lookup_historical.return_value = None

    estimate = explain_estimate("SELECT 1", warehouse="WH")

    expected_cost = round(
        credits_per_hour("X4LARGE") * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6
    )
    assert estimate.estimated_cost_usd == expected_cost
    assert any("could not be determined" in c for c in estimate.caveats)
    assert any("understated" in c for c in estimate.caveats)


@patch("cost_guard_mcp.engines.snowflake._lookup_historical_runtime")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_falls_back_to_gen1_rate_when_no_warehouse_can_be_resolved(
    mock_connect, mock_lookup_historical
):
    # warehouse=None and the session has no current warehouse either: nothing can be
    # observed, so the pre-existing Gen1/asserted fallback and caveat apply. Deliberately
    # does NOT mock _lookup_warehouse_facts - exercises the real resolution path.
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [(plan_json,), (None,)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    mock_lookup_historical.return_value = None

    estimate = explain_estimate("SELECT 1", warehouse=None, warehouse_size="MEDIUM")

    expected_cost = round(
        credits_per_hour("MEDIUM") * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6
    )
    assert estimate.estimated_cost_usd == expected_cost
    assert any("could not be determined" in c for c in estimate.caveats)
    assert any("no warehouse specified" in c for c in estimate.caveats)


def test_explain_estimate_runtime_caveat_names_the_effective_size(snowflake_fakes):
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("ANALYTICS_XL", "4X-Large")],
        warehouse="ANALYTICS_XL",
    )

    assert any("on a X4LARGE warehouse" in c for c in estimate.caveats)


# --- explain_estimate: edition comes from SNOWFLAKE_EDITION, the argument may only raise it --


def test_explain_estimate_prices_at_the_configured_edition_when_argument_omitted(
    snowflake_fakes, snowflake_config
):
    snowflake_config.return_value.edition = "enterprise"

    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small", generation="1")],
    )

    expected = round(credits_per_hour("XSMALL") * 3.00 * _ASSUMED_RUNTIME_HOURS, 6)
    assert estimate.estimated_cost_usd == expected
    assert any("SNOWFLAKE_EDITION" in c for c in estimate.caveats)


def test_explain_estimate_lets_the_caller_raise_the_edition(snowflake_fakes, snowflake_config):
    snowflake_config.return_value.edition = "enterprise"

    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small", generation="1")],
        edition="vps",
    )

    assert estimate.estimated_cost_usd == round(
        credits_per_hour("XSMALL") * 6.00 * _ASSUMED_RUNTIME_HOURS, 6
    )


def test_explain_estimate_never_lets_the_caller_lower_the_edition(
    snowflake_fakes, snowflake_config
):
    snowflake_config.return_value.edition = "business_critical"

    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small", generation="1")],
        edition="standard",
    )

    assert estimate.estimated_cost_usd == round(
        credits_per_hour("XSMALL") * 4.00 * _ASSUMED_RUNTIME_HOURS, 6
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"warehouse_size": "GIGANTIC"}, "unknown Snowflake warehouse size"),
        ({"edition": "platinum"}, "unknown Snowflake edition"),
    ],
)
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_validates_asserted_rate_inputs_before_connecting(
    mock_connect, kwargs, message
):
    # Validation-first ordering: the pricing ValueError fires before any connection or
    # warehouse lookup, so its (bounded) message is what the caller sees.
    with pytest.raises(SanitizedEngineError, match=message):
        explain_estimate("SELECT 1", warehouse="WH", **kwargs)

    mock_connect.assert_not_called()


# --- explain_estimate: bytesAssigned type guard (amendment A10) -----------------------------


def test_explain_estimate_returns_no_estimate_when_bytes_assigned_is_null(snowflake_fakes):
    # A null bytesAssigned used to flow into scale_runtime_hours as None and produce the
    # information-free baseline figure that DECISIONS.md #5 forbids - a dollar cap would
    # have looked enforced against a number that said nothing about the query.
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small")],
        bytes_assigned=None,
    )

    assert estimate.estimated_bytes is None
    assert estimate.estimated_cost_usd is None
    assert any("bytesAssigned" in c and "unavailable" in c for c in estimate.caveats)


def test_explain_estimate_keeps_the_float_for_an_integer_bytes_assigned(snowflake_fakes):
    # Positive control for the guard above.
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small")],
        bytes_assigned=1000,
    )

    assert estimate.estimated_bytes == 1000
    assert isinstance(estimate.estimated_cost_usd, float)


@pytest.mark.parametrize("bytes_assigned", [True, -1, "1000", 12.5])
def test_explain_estimate_rejects_non_integer_or_negative_bytes_assigned(
    snowflake_fakes, bytes_assigned
):
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small")],
        bytes_assigned=bytes_assigned,
    )

    assert estimate.estimated_bytes is None
    assert estimate.estimated_cost_usd is None


def test_explain_estimate_prices_from_history_when_bytes_assigned_is_null(snowflake_fakes):
    # The historical runtime is query-dependent evidence, so a dollar figure is still
    # legitimate without a byte estimate - but the byte estimate itself stays None.
    estimate, _ = _estimate_with(
        snowflake_fakes,
        show_rows=[snowflake_fakes.show_row("COMPUTE_WH", "X-Small", generation="1")],
        bytes_assigned=None,
        history_rows=[("SELECT 1", 4000, 1024)],
    )

    assert estimate.estimated_bytes is None
    assert estimate.estimated_cost_usd == round(
        credits_per_hour("XSMALL") * usd_per_credit("standard") * (4000 / 1000 / 3600), 6
    )


@patch("cost_guard_mcp.engines.snowflake._lookup_historical_runtime")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_uses_historical_signal_when_available(mock_connect, mock_lookup):
    # Integration-level wiring test (plan Task 2.1 Step 7): proves explain_estimate itself
    # picks the historical path when _lookup_historical_runtime returns a signal, rather than
    # just testing the isolated lookup function above.
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    mock_lookup.return_value = HistoricalRuntimeSignal(avg_runtime_hours=10 / 3600, sample_count=3)

    estimate = explain_estimate("SELECT 1", warehouse="WH", warehouse_size="XSMALL")

    expected_cost = round(credits_per_hour("XSMALL") * usd_per_credit("standard") * (10 / 3600), 6)
    assert estimate.estimated_cost_usd == expected_cost
    assert any("informed by 3 historical run(s)" in c for c in estimate.caveats)
    assert not any("Cost assumes a baseline" in c for c in estimate.caveats)


@patch("cost_guard_mcp.engines.snowflake._lookup_historical_runtime")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_explain_estimate_uses_tiered_heuristic_when_no_historical_signal(
    mock_connect, mock_lookup
):
    # Same wiring test, opposite branch: when _lookup_historical_runtime returns None, the
    # existing tiered-heuristic path and caveat must fire exactly as before this feature -
    # zero behavior change for the common no-match case.
    plan_json = json.dumps(
        {"GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}}
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (plan_json,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    mock_lookup.return_value = None

    estimate = explain_estimate("SELECT 1", warehouse="WH", warehouse_size="XSMALL")

    expected_cost = round(
        credits_per_hour("XSMALL") * usd_per_credit("standard") * _ASSUMED_RUNTIME_HOURS, 6
    )
    assert estimate.estimated_cost_usd == expected_cost
    assert any("Cost assumes a baseline" in c for c in estimate.caveats)
    assert not any("informed by" in c for c in estimate.caveats)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_ok_and_pins_warehouse_when_given(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "COST_GUARD_READER",
        '{"roles":"","value":""}',
        "COMPUTE_WH",
        "ABC12345",
    )
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials(warehouse="COMPUTE_WH")

    mock_connect.assert_called_once_with()  # read-only: default statement-timeout target
    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert "USE WAREHOUSE COMPUTE_WH" in executed_sql[0]
    assert result.engine == "snowflake"
    assert result.ok is True
    assert "COST_GUARD_READER" in result.detail
    assert "COMPUTE_WH" in result.detail
    assert "ABC12345" in result.detail


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_probes_and_reports_secondary_roles(mock_connect):
    # Security audit snowflake-role-pin-does-not-bound-session-authority-secondary-roles-
    # default-all: the operator's one in-tool identity check must show the effective
    # authority (primary role AND the active secondary-role set), not CURRENT_ROLE() alone.
    secondary = '{"roles":"","value":""}'
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("DUMMY_ROLE", secondary, "DUMMY_WH", "DUMMYACCT")
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials(warehouse=None)

    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert len(executed_sql) == 1
    assert "CURRENT_ROLE()" in executed_sql[0]
    assert "CURRENT_SECONDARY_ROLES()" in executed_sql[0]
    assert result.ok is True
    assert "DUMMY_ROLE" in result.detail
    assert "secondary roles" in result.detail
    assert secondary in result.detail
    assert "DUMMYACCT" in result.detail
    assert "DUMMY_WH" in result.detail


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_skips_use_warehouse_when_none(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "COST_GUARD_READER",
        '{"roles":"","value":""}',
        None,
        "ABC12345",
    )
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


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_closes_connection_on_success(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "COST_GUARD_READER",
        '{"roles":"","value":""}',
        "COMPUTE_WH",
        "ABC12345",
    )
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials()

    assert result.ok is True
    mock_conn.close.assert_called_once()


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_check_credentials_closes_connection_even_on_failure(mock_connect):
    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = RuntimeError("boom")
    mock_connect.return_value = mock_conn

    result = check_credentials()

    assert result.ok is False
    assert "boom" in result.detail
    mock_conn.close.assert_called_once()


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

    # The billable path asks for a session deadline matching its own 120 s watchdog, so the
    # warehouse-side query dies on its own even if this process is gone before cancel runs.
    mock_connect.assert_called_once_with(statement_timeout_seconds=_MAX_EXECUTION_WAIT_SECONDS)
    mock_conn.get_query_status_throw_if_error.assert_called_once_with(mock_cursor.sfqid)
    mock_cursor.get_results_from_sfqid.assert_called_once_with(mock_cursor.sfqid)
    assert rows == [{"a": 1}]
    assert row_count == 1
    assert row_cap_hit is False


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_pins_statement_count_to_one_on_execute_async(mock_connect, _mock_sleep):
    # Security audit snowflake-execute-unpinned-multi-statement-count: execute_async is the
    # billable call. It forwards **kwargs to execute, so num_statements=1 becomes
    # statement_params {'MULTI_STATEMENT_COUNT': 1} on the request (pinned by
    # test_snowflake_dependency_contract.py).
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = False
    mock_connect.return_value = mock_conn

    execute_bounded("SELECT 1", warehouse=None, max_rows=None)

    mock_cursor.execute_async.assert_called_once_with("SELECT 1", num_statements=1)


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_pins_statement_count_on_limit_wrapped_text_too(mock_connect, _mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_cursor.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = False
    mock_connect.return_value = mock_conn

    execute_bounded("SELECT * FROM t", warehouse=None, max_rows=5)

    mock_cursor.execute_async.assert_called_once_with(
        "SELECT * FROM (SELECT * FROM t) AS cost_guard_row_cap LIMIT 6", num_statements=1
    )


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_statement_count_rejection_propagates_and_closes_connection(
    mock_connect, _mock_sleep
):
    mock_cursor = MagicMock()
    mock_cursor.execute_async.side_effect = Exception(
        "Actual statement count 2 did not match the desired statement count 1."
    )
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="did not match the desired statement count"):
        execute_bounded("SELECT 1; CREATE TABLE cg_probe(x INT)", warehouse=None, max_rows=None)

    mock_conn.get_query_status.assert_not_called()
    mock_cursor.fetchall.assert_not_called()
    mock_conn.close.assert_called_once()


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


@pytest.mark.parametrize(
    ("raw_query_id", "canonical_query_id"),
    [
        ("{11111111-1111-1111-1111-111111111111}", "11111111-1111-1111-1111-111111111111"),
        ("11111111-1111-1111-1111-11111111111A", "11111111-1111-1111-1111-11111111111a"),
        (
            "urn:uuid:11111111-1111-1111-1111-111111111111",
            "11111111-1111-1111-1111-111111111111",
        ),
    ],
)
@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_cancel_interpolates_normalised_uuid_text(
    mock_connect, _mock_sleep, raw_query_id, canonical_query_id
):
    # Plan-review amendment A17: uuid.UUID() accepts braces/urn/upper-case forms, so
    # validating and then interpolating the RAW string still lets non-canonical text into
    # the SYSTEM$CANCEL_QUERY literal. Interpolate str(uuid.UUID(...)) - canonical only.
    mock_cursor = MagicMock()
    mock_cursor.sfqid = raw_query_id
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = True  # never finishes
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="was cancelled"):
        execute_bounded("SELECT * FROM huge_table", warehouse=None, max_rows=None)

    executed_sql = [call.args[0] for call in mock_cursor.execute.call_args_list]
    cancel_statements = [stmt for stmt in executed_sql if "SYSTEM$CANCEL_QUERY" in stmt]
    assert cancel_statements == [f"SELECT SYSTEM$CANCEL_QUERY('{canonical_query_id}')"]
    assert raw_query_id not in cancel_statements[0] or raw_query_id == canonical_query_id


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


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_closes_connection_on_success(mock_connect, _mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_cursor.fetchall.return_value = [{"a": 1}]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.side_effect = [True, False]
    mock_connect.return_value = mock_conn

    execute_bounded("SELECT 1", warehouse=None, max_rows=None)

    mock_conn.close.assert_called_once()


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_closes_connection_even_on_failure(mock_connect, _mock_sleep):
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.return_value = True  # never finishes -> times out
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="Query exceeded 120s and was cancelled"):
        execute_bounded("SELECT * FROM huge_table", warehouse=None, max_rows=None)

    mock_conn.close.assert_called_once()


@patch("cost_guard_mcp.engines.snowflake.time.sleep")
@patch("cost_guard_mcp.engines.snowflake._connect")
def test_execute_bounded_survives_a_failed_connection_close(mock_connect, _mock_sleep):
    # The outer finally's own conn.close() is best-effort, mirroring _cancel_query's own
    # discipline - a failed close must not mask a successful result.
    mock_cursor = MagicMock()
    mock_cursor.sfqid = "11111111-1111-1111-1111-111111111111"
    mock_cursor.fetchall.return_value = [{"a": 1}]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.is_still_running.side_effect = [True, False]
    mock_conn.close.side_effect = Exception("close also failed")
    mock_connect.return_value = mock_conn

    rows, row_count, row_cap_hit = execute_bounded("SELECT 1", warehouse=None, max_rows=None)

    assert rows == [{"a": 1}]
    assert row_count == 1
    assert row_cap_hit is False
    mock_conn.close.assert_called_once()
