import pytest

from cost_guard_mcp.config import ConfigError, load_bigquery_config, load_snowflake_config


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
