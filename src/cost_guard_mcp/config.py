import os
from dataclasses import dataclass, field

from cost_guard_mcp.pricing.snowflake_pricing import usd_per_credit


class ConfigError(RuntimeError):
    """Raised when a required credential/config env var is missing or invalid."""


_DEFAULT_SNOWFLAKE_EDITION = "standard"


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
    # Login-time SECONDARY_ROLES pin: "NONE" (default) or "ALL" (explicit opt-out). See
    # load_snowflake_config() and DECISIONS.md #7.
    secondary_roles: str = "NONE"
    # Account edition (SNOWFLAKE_EDITION) sets the USD/credit price of every estimate. It is
    # operator configuration because the server cannot observe it without ORGADMIN; the
    # per-call `edition` tool argument may only raise it (DECISIONS.md #19).
    edition: str = _DEFAULT_SNOWFLAKE_EDITION


@dataclass(frozen=True)
class DatabricksConfig:
    server_hostname: str
    http_path: str
    access_token: str | None = field(repr=False)
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)


def load_bigquery_config() -> BigQueryConfig:
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds:
        raise ConfigError(
            "GOOGLE_APPLICATION_CREDENTIALS is not set. cost-guard-mcp requires "
            "Application Default Credentials for BigQuery — set this env var to a "
            "service-account key file path, or run `gcloud auth application-default login`."
        )
    return BigQueryConfig(google_application_credentials=creds)


# Account-wide administrative roles that SNOWFLAKE_ROLE may never be set to. DECISIONS.md #7,
# README and server.json have always promised "never ACCOUNTADMIN"; this makes the promise a
# load-time check instead of prose. SECURITYADMIN and ORGADMIN carry the same class of
# account-wide authority (grant management / organization administration) and are rejected
# for the same reason. Compared after .strip().strip('"').upper() so a quoted or lower-case
# env value cannot slip past (security audit, plan-review amendment A17).
_REJECTED_SNOWFLAKE_ADMIN_ROLES = frozenset({"ACCOUNTADMIN", "SECURITYADMIN", "ORGADMIN"})

# The only values Snowflake's login-time SECONDARY_ROLES accepts. "NONE" is the secure default:
# without an explicit pin, Snowflake activates the user's DEFAULT_SECONDARY_ROLES - documented
# as ('ALL') for users created after behavior-change bundle 2024_07 - so model-supplied SQL
# would be authorized by EVERY role granted to SNOWFLAKE_USER, not by SNOWFLAKE_ROLE alone.
_ALLOWED_SNOWFLAKE_SECONDARY_ROLES = frozenset({"NONE", "ALL"})


def _normalize_snowflake_role_name(role: str) -> str:
    return role.strip().strip('"').upper()


def _load_snowflake_secondary_roles() -> str:
    # An empty string (what CI / secret stores set for a missing value) is treated as unset,
    # exactly like the empty-passphrase handling below - it falls to the secure default.
    raw = os.environ.get("SNOWFLAKE_SECONDARY_ROLES", "").strip().upper()
    if not raw:
        return "NONE"
    if raw not in _ALLOWED_SNOWFLAKE_SECONDARY_ROLES:
        raise ConfigError(
            "SNOWFLAKE_SECONDARY_ROLES must be either NONE (default: only SNOWFLAKE_ROLE is "
            "active in the session) or ALL (opt-out: every role granted to SNOWFLAKE_USER is "
            "active). Individual role names are not accepted here - grant the objects to "
            "SNOWFLAKE_ROLE instead."
        )
    return raw


def _parse_snowflake_edition(raw: str | None) -> str:
    """Normalise SNOWFLAKE_EDITION (unset/empty -> 'standard', case-insensitive) and validate
    it against the pricing table at load time, so a typo is a clear ConfigError on the first
    tool call rather than a pricing ValueError inside an estimate."""
    edition = (raw or "").strip().lower() or _DEFAULT_SNOWFLAKE_EDITION
    try:
        usd_per_credit(edition)
    except ValueError as exc:
        raise ConfigError(f"SNOWFLAKE_EDITION is invalid: {exc}") from exc
    return edition


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
    if _normalize_snowflake_role_name(role) in _REJECTED_SNOWFLAKE_ADMIN_ROLES:
        # Static text on purpose: the value is operator config, but this message reaches
        # the MCP client as a ToolError, so it never echoes the env value back.
        raise ConfigError(
            "SNOWFLAKE_ROLE must not be an account-wide administrative role (ACCOUNTADMIN, "
            "SECURITYADMIN or ORGADMIN) - cost-guard-mcp runs model-supplied SQL under this "
            "role. Set it to a least-privilege role scoped to the objects you want "
            "cost-estimated."
        )

    secondary_roles = _load_snowflake_secondary_roles()

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
        # `or None` (not a bare os.environ.get()): GitHub Actions sets a workflow env var
        # to an empty string, not unset, when the secret it references does not exist -
        # confirmed live in CI (integration.yml's snowflake job) - and
        # snowflake-connector-python treats an empty-but-non-None private_key_file_pwd as
        # "a password was given", raising "Password was given but private key is not
        # encrypted" against a real, unencrypted key. An empty passphrase is never
        # meaningfully different from no passphrase.
        private_key_passphrase=os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None,
        password=password,
        secondary_roles=secondary_roles,
        edition=_parse_snowflake_edition(os.environ.get("SNOWFLAKE_EDITION")),
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
