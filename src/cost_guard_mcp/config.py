import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when a required credential/config env var is missing or invalid."""


@dataclass(frozen=True)
class BigQueryConfig:
    google_application_credentials: str


@dataclass(frozen=True)
class SnowflakeConfig:
    account: str
    user: str
    role: str
    private_key_path: str | None
    private_key_passphrase: str | None = field(repr=False)
    password: str | None = field(repr=False)


@dataclass(frozen=True)
class DatabricksConfig:
    server_hostname: str
    http_path: str
    access_token: str | None
    client_id: str | None
    client_secret: str | None = field(repr=False)


def load_bigquery_config() -> BigQueryConfig:
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds:
        raise ConfigError(
            "GOOGLE_APPLICATION_CREDENTIALS is not set. cost-guard-mcp requires "
            "Application Default Credentials for BigQuery — set this env var to a "
            "service-account key file path, or run `gcloud auth application-default login`."
        )
    return BigQueryConfig(google_application_credentials=creds)


def load_snowflake_config() -> SnowflakeConfig:
    account = os.environ.get("SNOWFLAKE_ACCOUNT")
    user = os.environ.get("SNOWFLAKE_USER")
    if not account:
        raise ConfigError("SNOWFLAKE_ACCOUNT must be set.")
    if not user:
        raise ConfigError("SNOWFLAKE_USER must be set.")

    role = os.environ.get("SNOWFLAKE_ROLE")
    if not role:
        raise ConfigError(
            "SNOWFLAKE_ROLE must be set explicitly — cost-guard-mcp does not default "
            "to any role (in particular, never ACCOUNTADMIN). Set it to a "
            "least-privilege role scoped to the objects you want cost-estimated."
        )

    private_key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    password = os.environ.get("SNOWFLAKE_PASSWORD")
    if not private_key_path and not password:
        raise ConfigError(
            "Set either SNOWFLAKE_PRIVATE_KEY_PATH (preferred, key-pair auth) or "
            "SNOWFLAKE_PASSWORD (discouraged, logged as a startup warning) to authenticate."
        )

    return SnowflakeConfig(
        account=account,
        user=user,
        role=role,
        private_key_path=private_key_path,
        private_key_passphrase=os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
        password=password,
    )


def load_databricks_config() -> DatabricksConfig:
    server_hostname = os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    if not server_hostname:
        raise ConfigError(
            "DATABRICKS_SERVER_HOSTNAME must be set (e.g. "
            "my-workspace.cloud.databricks.com — no https:// prefix, no trailing slash)."
        )

    http_path = os.environ.get("DATABRICKS_HTTP_PATH")
    if not http_path:
        raise ConfigError(
            "DATABRICKS_HTTP_PATH must be set to the SQL warehouse's HTTP path "
            "(from the warehouse's Connection Details tab, e.g. "
            "/sql/1.0/warehouses/<warehouse-id>). This selects which SQL warehouse "
            "cost-guard-mcp connects to — Databricks has no per-query USE WAREHOUSE "
            "equivalent to switch warehouses within a session."
        )

    access_token = os.environ.get("DATABRICKS_TOKEN")
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET")

    if not access_token and not client_id:
        raise ConfigError(
            "Set either DATABRICKS_TOKEN (a personal access token, simplest) or "
            "DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET (OAuth machine-to-machine "
            "via a service principal, preferred for automated use) to authenticate."
        )
    if client_id and not client_secret:
        raise ConfigError(
            "DATABRICKS_CLIENT_ID is set but DATABRICKS_CLIENT_SECRET is not — "
            "OAuth M2M needs both."
        )

    return DatabricksConfig(
        server_hostname=server_hostname,
        http_path=http_path,
        access_token=access_token,
        client_id=client_id,
        client_secret=client_secret,
    )
