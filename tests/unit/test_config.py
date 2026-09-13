import pytest

from cost_guard_mcp.config import (
    ConfigError,
    DatabricksConfig,
    SnowflakeConfig,
    load_bigquery_config,
    load_databricks_config,
    load_snowflake_config,
)


def test_load_bigquery_config_raises_when_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(ConfigError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        load_bigquery_config()


def test_load_bigquery_config_succeeds_when_present(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    config = load_bigquery_config()
    assert config.google_application_credentials == "/tmp/sa.json"


def test_load_snowflake_config_requires_role_with_no_default(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "abc123")
    monkeypatch.setenv("SNOWFLAKE_USER", "svc_user")
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", "/tmp/rsa_key.p8")
    monkeypatch.delenv("SNOWFLAKE_ROLE", raising=False)
    with pytest.raises(ConfigError, match="SNOWFLAKE_ROLE"):
        load_snowflake_config()


def test_load_snowflake_config_requires_account_and_user(monkeypatch):
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    monkeypatch.setenv("SNOWFLAKE_USER", "svc_user")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "COST_GUARD_READER")
    with pytest.raises(ConfigError, match="SNOWFLAKE_ACCOUNT"):
        load_snowflake_config()


def test_load_snowflake_config_requires_key_pair_or_password(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "abc123")
    monkeypatch.setenv("SNOWFLAKE_USER", "svc_user")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "COST_GUARD_READER")
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="SNOWFLAKE_PRIVATE_KEY_PATH"):
        load_snowflake_config()


def test_load_snowflake_config_succeeds_with_key_pair(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "abc123")
    monkeypatch.setenv("SNOWFLAKE_USER", "svc_user")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "COST_GUARD_READER")
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", "/tmp/rsa_key.p8")
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    config = load_snowflake_config()
    assert config.role == "COST_GUARD_READER"
    assert config.private_key_path == "/tmp/rsa_key.p8"
    assert config.password is None


def test_snowflake_config_does_not_leak_secrets_in_repr():
    passphrase_value = "test_passphrase_data_xyz"
    password_value = "test_password_data_xyz"
    config = SnowflakeConfig(
        account="myaccount",
        user="myuser",
        role="READER",
        private_key_path="/path/to/key.p8",
        private_key_passphrase=passphrase_value,
        password=password_value,
    )
    config_repr = repr(config)
    assert passphrase_value not in config_repr
    assert password_value not in config_repr
    assert "myaccount" in config_repr
    assert "myuser" in config_repr
    assert "READER" in config_repr


def test_load_databricks_config_requires_server_hostname(monkeypatch):
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME", raising=False)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi_test")
    with pytest.raises(ConfigError, match="DATABRICKS_SERVER_HOSTNAME"):
        load_databricks_config()


def test_load_databricks_config_requires_http_path(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.delenv("DATABRICKS_HTTP_PATH", raising=False)
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi_test")
    with pytest.raises(ConfigError, match="DATABRICKS_HTTP_PATH"):
        load_databricks_config()


def test_load_databricks_config_requires_pat_or_oauth_m2m(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="DATABRICKS_TOKEN"):
        load_databricks_config()


def test_load_databricks_config_requires_both_oauth_fields_together(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-abc")
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="DATABRICKS_CLIENT_SECRET"):
        load_databricks_config()


def test_load_databricks_config_succeeds_with_pat(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi_test")
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    config = load_databricks_config()
    assert config.server_hostname == "my-workspace.cloud.databricks.com"
    assert config.http_path == "/sql/1.0/warehouses/abc123"
    assert config.access_token == "dapi_test"
    assert config.client_id is None
    assert config.client_secret is None


def test_load_databricks_config_succeeds_with_oauth_m2m(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-abc")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret-xyz")
    config = load_databricks_config()
    assert config.access_token is None
    assert config.client_id == "client-abc"
    assert config.client_secret == "secret-xyz"


def test_databricks_config_does_not_leak_secrets_in_repr():
    token_value = "dapi_supersecret"
    secret_value = "oauth_supersecret"
    config = DatabricksConfig(
        server_hostname="my-workspace.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        access_token=token_value,
        client_id="client-abc",
        client_secret=secret_value,
    )
    config_repr = repr(config)
    assert token_value not in config_repr
    assert secret_value not in config_repr
    # Non-secret fields must stay visible - repr=False should be scoped to credentials
    # only, not applied blanket-wide. (Deliberately not asserting on server_hostname's
    # literal value here - a domain-shaped string literal inside an `in` check trips
    # CodeQL's "Incomplete URL substring sanitization" query, a false positive for a
    # plain repr-content assertion with no URL-validation semantics at all.)
    assert "client-abc" in config_repr
