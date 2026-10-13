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

import inspect

from snowflake.connector.connection import DEFAULT_CONFIGURATION
from snowflake.connector.cursor import SnowflakeCursor


def test_connector_accepts_secondary_roles_connect_kwarg():
    # Security audit snowflake-role-pin-does-not-bound-session-authority-secondary-roles-
    # default-all: _connect() passes secondary_roles=config.secondary_roles so the login
    # request carries SECONDARY_ROLES=NONE (or the ALL opt-out) before any SQL runs.
    assert "secondary_roles" in DEFAULT_CONFIGURATION
    default_value, accepted_types = DEFAULT_CONFIGURATION["secondary_roles"]
    assert default_value is None
    assert str in accepted_types


def test_connector_accepts_session_parameters_connect_kwarg():
    # Security audit snowflake-execute-unpinned-multi-statement-count (belt and braces half):
    # _connect() passes session_parameters={"MULTI_STATEMENT_COUNT": 1} so the pin also
    # travels in the login body and covers any future execute added without the kwarg.
    assert "session_parameters" in DEFAULT_CONFIGURATION
    default_value, accepted_types = DEFAULT_CONFIGURATION["session_parameters"]
    assert default_value is None
    assert dict in accepted_types


def test_cursor_execute_accepts_num_statements():
    # The per-call half: cursor.execute turns num_statements into
    # statement_params {"MULTI_STATEMENT_COUNT": n} on the request.
    assert "num_statements" in inspect.signature(SnowflakeCursor.execute).parameters


def test_cursor_execute_async_forwards_kwargs_to_execute():
    # Plan-review amendment A13: execute_async's own signature has NO num_statements
    # parameter - the pin on the billable execute_bounded path works only because
    # execute_async forwards **kwargs to self.execute. A connector refactor could drop that
    # silently (the connector logs, not raises, on unknown parameters), so both halves are
    # pinned: the catch-all signature AND the forwarding call in the source.
    params = list(inspect.signature(SnowflakeCursor.execute_async).parameters.values())
    kinds = [param.kind for param in params if param.name != "self"]
    assert kinds == [inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD]
    assert "self.execute(" in inspect.getsource(SnowflakeCursor.execute_async)
