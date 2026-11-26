"""A SNOWFLAKE_ROLE that is blank once whitespace and quotes are stripped is refused as unset.

Before the fix such a value passed the "is it set" check (the raw text is truthy) and the
admin-role check (the empty string is in no admin set) and reached the connector unchanged.
"""

import pytest

from cost_guard_mcp.config import ConfigError, load_snowflake_config


@pytest.mark.parametrize(
    "role",
    ["    ", "\t\n", "\u00a0", "''", '""', '""""', " ' ' ", "  '  \"  '  "],
)
def test_role_that_is_blank_after_normalisation_is_refused_as_unset(monkeypatch, role):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "test_account")
    monkeypatch.setenv("SNOWFLAKE_USER", "test_user")
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", "/tmp/fake_key.p8")
    monkeypatch.setenv("SNOWFLAKE_ROLE", role)

    with pytest.raises(ConfigError, match="SNOWFLAKE_ROLE must be set explicitly"):
        load_snowflake_config()
