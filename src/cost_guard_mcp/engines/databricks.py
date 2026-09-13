from databricks import sql
from databricks.sdk.core import Config, oauth_service_principal
from databricks.sdk.credentials_provider import OAuthCredentialsProvider

from cost_guard_mcp.config import load_databricks_config
from cost_guard_mcp.errors import sanitize_exceptions

# databricks-sql-connector's own _socket_timeout defaults to 900s on the backend used
# here (confirmed via CONNECTION_PARAMETERS.md against the installed package) - not
# infinite, but still too long to leave implicit. Set explicitly, matching this
# project's own established discipline (see snowflake.py's _NETWORK_TIMEOUT_SECONDS).
#
# Deliberately do NOT pass use_kernel=True: the newer Rust "kernel" backend rejects a
# generic credentials_provider (OAuth M2M) with NotSupportedError. The default (legacy
# Thrift) backend used here supports both PAT and OAuth M2M.
_SOCKET_TIMEOUT_SECONDS = 60


@sanitize_exceptions("databricks")
def _connect() -> "sql.client.Connection":
    config = load_databricks_config()

    if config.client_id:
        sdk_config = Config(
            host=f"https://{config.server_hostname}",
            client_id=config.client_id,
            client_secret=config.client_secret,
        )

        def credentials_provider() -> OAuthCredentialsProvider:
            return oauth_service_principal(sdk_config)

        return sql.connect(
            server_hostname=config.server_hostname,
            http_path=config.http_path,
            credentials_provider=credentials_provider,
            _socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        )

    return sql.connect(
        server_hostname=config.server_hostname,
        http_path=config.http_path,
        access_token=config.access_token,
        _socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )
