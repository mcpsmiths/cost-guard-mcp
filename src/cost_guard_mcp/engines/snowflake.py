import snowflake.connector

from cost_guard_mcp.config import load_snowflake_config
from cost_guard_mcp.errors import sanitize_exceptions


@sanitize_exceptions("snowflake")
def _connect() -> "snowflake.connector.SnowflakeConnection":
    config = load_snowflake_config()

    if config.private_key_path:
        return snowflake.connector.connect(
            account=config.account,
            user=config.user,
            role=config.role,
            authenticator="SNOWFLAKE_JWT",
            private_key_file=config.private_key_path,
            private_key_file_pwd=config.private_key_passphrase,
        )

    return snowflake.connector.connect(
        account=config.account,
        user=config.user,
        role=config.role,
        password=config.password,
    )
