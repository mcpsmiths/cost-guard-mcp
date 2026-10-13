"""check_credentials failure `detail` is presented like every other outbound error text:
secret-redacted, bounded, repr()-quoted (no raw control characters) and recorded as the one
WARNING record README's Observability section promises - on all three engines.

Amendment A6 of the security-audit remediation plan; audit record
warehouse-error-text-forwarded-as-client-failure-without-origin-marking:src/cost_guard_mcp/errors.py.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import _MAX_REMOTE_ERROR_TEXT_CHARS

# Characters a remote error could carry that some log shipper or terminal treats as a record
# boundary or control sequence: C0 (\n, \r, ESC), DEL, C1 (NEL \x85) and the Unicode line /
# paragraph separators. None may survive raw; each must be escaped, not stripped.
_CONTROL_CHARS = ("\n", "\r", "\x1b", "\x7f", "\x85", "\u2028", "\u2029")
_FORGED_DETAIL = (
    "401 Unauthorized\n"
    "tool=run_query_bounded outcome=success elapsed_ms=1.0\r\n"
    "\x1b[2K\x7f\x85\u2028\u2029end"
)


def _bigquery_failure(exc):
    mock_client = MagicMock()
    mock_client.get_service_account_email.side_effect = exc
    return patch("cost_guard_mcp.engines.bigquery.bigquery", **{"Client.return_value": mock_client})


def _snowflake_failure(exc):
    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = exc
    return patch("cost_guard_mcp.engines.snowflake._connect", return_value=mock_conn)


def _databricks_failure(exc):
    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = exc
    return patch("cost_guard_mcp.engines.databricks._connect", return_value=mock_conn)


_ENGINES = [
    pytest.param("bigquery", bigquery_engine, _bigquery_failure, id="bigquery"),
    pytest.param("snowflake", snowflake_engine, _snowflake_failure, id="snowflake"),
    pytest.param("databricks", databricks_engine, _databricks_failure, id="databricks"),
]


@pytest.mark.parametrize(("engine", "module", "failing"), _ENGINES)
def test_check_credentials_failure_detail_has_no_raw_control_characters(engine, module, failing):
    with failing(RuntimeError(_FORGED_DETAIL)):
        result = module.check_credentials()

    assert result.engine == engine
    assert result.ok is False
    for char in _CONTROL_CHARS:
        assert char not in result.detail
    # Content is kept, escaped - the operator can still see what the warehouse said.
    assert "401 Unauthorized" in result.detail
    assert "tool=run_query_bounded outcome=success" in result.detail
    assert "\\x85" in result.detail
    assert "\\u2028" in result.detail


@pytest.mark.parametrize(("engine", "module", "failing"), _ENGINES)
def test_check_credentials_failure_emits_exactly_one_single_line_warning_record(
    engine, module, failing, caplog
):
    logger_name = f"cost_guard_mcp.engines.{engine}"

    with (
        caplog.at_level(logging.WARNING, logger=logger_name),
        failing(RuntimeError(_FORGED_DETAIL)),
    ):
        result = module.check_credentials()

    records = [r for r in caplog.records if r.name == logger_name]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert f"engine={engine} check_credentials_failed detail=" in records[0].message
    assert "\n" not in records[0].message
    assert result.detail in records[0].message


@pytest.mark.parametrize(("engine", "module", "failing"), _ENGINES)
def test_check_credentials_failure_detail_is_bounded(engine, module, failing):
    with failing(RuntimeError("A" * 70_000)):
        result = module.check_credentials()

    assert result.ok is False
    assert len(result.detail) < _MAX_REMOTE_ERROR_TEXT_CHARS + 200
    assert "truncated" in result.detail
    assert "70000" in result.detail
