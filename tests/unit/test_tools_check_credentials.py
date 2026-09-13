from unittest.mock import patch

import pytest

from cost_guard_mcp.tools.check_credentials import check_credentials
from cost_guard_mcp.types import CredentialCheckResult


@patch("cost_guard_mcp.tools.check_credentials.bigquery_engine")
def test_check_credentials_dispatches_to_bigquery(mock_engine):
    mock_engine.check_credentials.return_value = CredentialCheckResult(
        engine="bigquery", ok=True, detail="Authenticated."
    )
    result = check_credentials("bigquery")
    mock_engine.check_credentials.assert_called_once_with()
    assert result.engine == "bigquery"
    assert result.ok is True


@patch("cost_guard_mcp.tools.check_credentials.snowflake_engine")
def test_check_credentials_dispatches_to_snowflake_with_warehouse(mock_engine):
    mock_engine.check_credentials.return_value = CredentialCheckResult(
        engine="snowflake", ok=True, detail="Authenticated."
    )
    result = check_credentials("snowflake", warehouse="COMPUTE_WH")
    mock_engine.check_credentials.assert_called_once_with("COMPUTE_WH")
    assert result.engine == "snowflake"


@patch("cost_guard_mcp.tools.check_credentials.databricks_engine")
def test_check_credentials_dispatches_to_databricks(mock_engine):
    mock_engine.check_credentials.return_value = CredentialCheckResult(
        engine="databricks", ok=True, detail="Authenticated."
    )
    result = check_credentials("databricks", warehouse="ignored")
    mock_engine.check_credentials.assert_called_once_with("ignored")
    assert result.engine == "databricks"


def test_check_credentials_rejects_unsupported_engine():
    with pytest.raises(ValueError, match="redshift"):
        check_credentials("redshift")  # type: ignore[arg-type]
