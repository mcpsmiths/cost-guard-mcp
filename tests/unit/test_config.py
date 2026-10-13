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


def test_load_snowflake_config_requires_user_when_account_present(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "abc123")
    monkeypatch.delenv("SNOWFLAKE_USER", raising=False)
    with pytest.raises(ConfigError, match="SNOWFLAKE_USER"):
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


def test_load_snowflake_config_treats_empty_passphrase_env_as_none(monkeypatch):
    # Regression test: GitHub Actions sets a workflow env var referencing a non-existent
    # secret to an empty string, not unset - confirmed live against integration.yml's
    # snowflake job, which raised "Password was given but private key is not encrypted"
    # because "" was passed straight through as a real passphrase.
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "abc123")
    monkeypatch.setenv("SNOWFLAKE_USER", "svc_user")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "COST_GUARD_READER")
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", "/tmp/rsa_key.p8")
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    config = load_snowflake_config()
    assert config.private_key_passphrase is None


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


def _set_minimal_snowflake_env(monkeypatch, role="COST_GUARD_READER"):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "abc123")
    monkeypatch.setenv("SNOWFLAKE_USER", "svc_user")
    monkeypatch.setenv("SNOWFLAKE_ROLE", role)
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", "/tmp/rsa_key.p8")
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)


def test_load_snowflake_config_secondary_roles_default_to_none(monkeypatch):
    # Security audit snowflake-role-pin-does-not-bound-session-authority-secondary-roles-
    # default-all: without an explicit pin, Snowflake activates the user's
    # DEFAULT_SECONDARY_ROLES (documented default ('ALL')), so SNOWFLAKE_ROLE alone never
    # bounded what model-supplied SQL could touch. Unset must mean NONE, not "whatever the
    # provider default is".
    _set_minimal_snowflake_env(monkeypatch)
    monkeypatch.delenv("SNOWFLAKE_SECONDARY_ROLES", raising=False)
    config = load_snowflake_config()
    assert config.secondary_roles == "NONE"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("all", "ALL"), ("none", "NONE"), (" ALL ", "ALL"), ("None", "NONE")],
)
def test_load_snowflake_config_secondary_roles_normalises_case_and_whitespace(
    monkeypatch, raw, expected
):
    _set_minimal_snowflake_env(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_SECONDARY_ROLES", raw)
    assert load_snowflake_config().secondary_roles == expected


def test_load_snowflake_config_secondary_roles_treats_empty_env_as_unset(monkeypatch):
    # Same CI shape as the empty-passphrase regression above: a workflow env var referencing
    # a missing value is "", not unset - and "" must fall to the secure default, never error.
    _set_minimal_snowflake_env(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_SECONDARY_ROLES", "")
    assert load_snowflake_config().secondary_roles == "NONE"


def test_load_snowflake_config_rejects_unknown_secondary_roles_value(monkeypatch):
    # Snowflake's login-time SECONDARY_ROLES accepts only ALL or NONE; a role name here is a
    # misconfiguration that must fail loudly, naming the variable and the two legal values.
    _set_minimal_snowflake_env(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_SECONDARY_ROLES", "SYSADMIN")
    with pytest.raises(ConfigError, match="SNOWFLAKE_SECONDARY_ROLES") as excinfo:
        load_snowflake_config()
    assert "NONE" in str(excinfo.value)
    assert "ALL" in str(excinfo.value)


@pytest.mark.parametrize(
    "role",
    [
        "ACCOUNTADMIN",
        " accountadmin ",
        '"SECURITYADMIN"',
        "securityadmin",
        "ORGADMIN",
        '"orgadmin"',
        # PR #86 runtime verification (T5): SNOWFLAKE_ROLE="'ACCOUNTADMIN'" - single quotes, the
        # other quoting style a shell or secret store can leave in an env value - passed the
        # normaliser (which stripped only double quotes) and reached connect(role=...).
        "'ACCOUNTADMIN'",
        " 'orgadmin' ",
        "'\"SECURITYADMIN\"'",
    ],
)
def test_load_snowflake_config_rejects_account_wide_admin_roles(monkeypatch, role):
    # DECISIONS.md #7 / README / server.json all promise "never ACCOUNTADMIN"; until now that
    # was prose only. The comparison is normalised (strip, unquote, upper) so a quoted or
    # lower-case env value cannot slip past it (plan-review amendment A17).
    _set_minimal_snowflake_env(monkeypatch, role=role)
    with pytest.raises(ConfigError, match="SNOWFLAKE_ROLE"):
        load_snowflake_config()


def test_load_snowflake_config_admin_role_error_text_is_static(monkeypatch):
    # The rejection message must not echo the env value back (it is operator config, but the
    # text reaches the MCP client via ToolError) - every rejected role gets identical text.
    messages = set()
    for role in ("ACCOUNTADMIN", '"orgadmin"', " SecurityAdmin ", "'accountadmin'"):
        _set_minimal_snowflake_env(monkeypatch, role=role)
        with pytest.raises(ConfigError) as excinfo:
            load_snowflake_config()
        messages.add(str(excinfo.value))
    assert len(messages) == 1


def test_load_snowflake_config_accepts_non_admin_role_unchanged(monkeypatch):
    _set_minimal_snowflake_env(monkeypatch, role="SYSADMIN")
    assert load_snowflake_config().role == "SYSADMIN"


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


def test_load_snowflake_config_edition_defaults_to_standard(monkeypatch):
    _set_minimal_snowflake_env(monkeypatch)
    monkeypatch.delenv("SNOWFLAKE_EDITION", raising=False)
    assert load_snowflake_config().edition == "standard"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("enterprise", "enterprise"),
        ("Business_Critical", "business_critical"),
        (" VPS ", "vps"),
        ("", "standard"),
    ],
)
def test_load_snowflake_config_normalises_snowflake_edition(monkeypatch, raw, expected):
    _set_minimal_snowflake_env(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_EDITION", raw)
    assert load_snowflake_config().edition == expected


def test_load_snowflake_config_rejects_an_unknown_snowflake_edition(monkeypatch):
    # Validated at load time against the pricing table, so a typo surfaces as a clear
    # ConfigError on the first tool call instead of a pricing ValueError deep inside an
    # estimate.
    _set_minimal_snowflake_env(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_EDITION", "platinum")
    with pytest.raises(ConfigError, match="SNOWFLAKE_EDITION"):
        load_snowflake_config()


def test_snowflake_config_edition_defaults_to_standard_when_constructed_directly():
    config = SnowflakeConfig(
        account="myaccount",
        user="myuser",
        role="READER",
        private_key_path="/path/to/key.p8",
        private_key_passphrase=None,
        password=None,
    )
    assert config.edition == "standard"
