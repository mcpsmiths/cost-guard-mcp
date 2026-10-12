"""Dependency-contract pins for the Snowflake session-authority and statement-count controls.

Mirrors tests/unit/test_databricks_dependency_contract.py. The controls in
cost_guard_mcp.engines.snowflake._connect are enforced by snowflake-connector-python at
login time, and the connector is fail-open about unknown connect() kwargs: it logs a
warning and ignores them (connection.py, the DEFAULT_CONFIGURATION check) rather than
raising. A dependency floor change that dropped a parameter would therefore turn a security
pin into a no-op with nothing else catching it. These tests make that a unit failure.

Verified against snowflake-connector-python 4.8.0 (uv.lock); `secondary_roles` dates from
4.3.0, so the project's >=4.7.1 floor already covers it.
"""

from snowflake.connector.connection import DEFAULT_CONFIGURATION


def test_connector_accepts_secondary_roles_connect_kwarg():
    # Security audit snowflake-role-pin-does-not-bound-session-authority-secondary-roles-
    # default-all: _connect() passes secondary_roles=config.secondary_roles so the login
    # request carries SECONDARY_ROLES=NONE (or the ALL opt-out) before any SQL runs.
    assert "secondary_roles" in DEFAULT_CONFIGURATION
    default_value, accepted_types = DEFAULT_CONFIGURATION["secondary_roles"]
    assert default_value is None
    assert str in accepted_types
