from unittest.mock import MagicMock, patch

from cost_guard_mcp.engines.databricks import _connect

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
