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
