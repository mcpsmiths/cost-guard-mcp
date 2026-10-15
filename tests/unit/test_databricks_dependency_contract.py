"""Regression test for Task 8.1 Step 2's verification spike.

The plan's Step 2 requires confirming the real, installed `databricks-sql-connector`/
`databricks-sdk` API surface matches what `cost_guard_mcp.engines.databricks._connect`
assumes, rather than trusting docs or research alone. That check was run manually once
and never encoded anywhere durable — so a future dependency bump could silently break
`_connect()` with nothing catching it. These tests pin the same contract permanently.

`sql.connect`'s public signature is `(server_hostname, http_path, access_token=None,
**kwargs)` in the installed version - `credentials_provider` and `_socket_timeout` are
swallowed generically by `**kwargs`, so passing a renamed/removed kwarg would NOT raise a
TypeError at the call site; it would silently do nothing. Per the plan's own fallback
instruction for this case ("check CONNECTION_PARAMETERS.md in the installed package's own
repo/README for confirmation instead"), these tests walk the installed package's actual
source for the exact code path `_connect()` uses (the default, non-`use_kernel` Thrift
auth flow — `_connect()` deliberately never passes `use_kernel=True`) to confirm both
kwargs are genuinely consumed there, and that `oauth_service_principal` still takes a
single `Config` argument.
"""

import inspect

import databricks.sql.backend.sea.utils.constants as databricks_sea_constants
import databricks.sql.backend.thrift_backend as databricks_thrift_backend
import databricks.sql.client as databricks_sql_client
from databricks.sdk.core import Config, oauth_service_principal
from databricks.sql.auth.auth import get_python_sql_connector_auth_provider


def test_sql_connect_hides_its_real_kwargs_behind_a_catchall():
    # Documents WHY the next test can't just do inspect.signature(sql.connect) and
    # check for "credentials_provider"/"_socket_timeout" by name.
    from databricks import sql

    params = inspect.signature(sql.connect).parameters
    assert "kwargs" in params
    assert params["kwargs"].kind is inspect.Parameter.VAR_KEYWORD


def test_socket_timeout_kwarg_is_read_by_the_installed_client():
    source = inspect.getsource(databricks_sql_client)
    assert '"_socket_timeout"' in source


def test_credentials_provider_kwarg_is_read_on_the_non_kernel_auth_path():
    # _connect() never passes use_kernel=True, so this function - not the kernel
    # bridge - is the one that must read credentials_provider for both PAT and OAuth
    # M2M connections to work.
    source = inspect.getsource(get_python_sql_connector_auth_provider)
    assert "credentials_provider" in source


def test_oauth_service_principal_takes_a_single_config_argument():
    params = list(inspect.signature(oauth_service_principal).parameters.values())
    assert len(params) == 1
    assert params[0].annotation in (Config, "Config")


# Audit: databricks-readonly-paths-inherit-connector-900s-retry-duration-and-sdk-300s-
# discovery-budget-no-project-deadline. _connect() now passes the connector's retry budget,
# a session_configuration STATEMENT_TIMEOUT and the SDK's own timeouts/auth_type. sql.connect
# swallows unknown kwargs via **kwargs, so a connector rename would silently restore the
# 900 s / 30-attempt defaults - these pins are what makes such a rename visible.


def test_retry_budget_kwargs_are_read_by_the_installed_thrift_backend():
    source = inspect.getsource(databricks_thrift_backend)
    assert '"_retry_stop_after_attempts_duration"' in source
    assert '"_retry_stop_after_attempts_count"' in source


def test_session_configuration_is_a_real_connection_parameter():
    params = inspect.signature(databricks_sql_client.Connection.__init__).parameters
    assert "session_configuration" in params


def test_statement_timeout_is_in_the_connectors_own_session_conf_allowlist():
    # The connector's allowlist is sourced from the Databricks SQL parameter reference; the
    # Thrift backend forwards session_configuration to OpenSession verbatim, so this is the
    # one offline check that the key is a real server-side session parameter.
    source = inspect.getsource(databricks_sea_constants)
    assert '"STATEMENT_TIMEOUT"' in source


def test_sdk_config_exposes_the_bounds_and_auth_type_connect_sets():
    for attribute in ("retry_timeout_seconds", "http_timeout_seconds", "auth_type"):
        assert hasattr(Config, attribute), attribute


# #94: execute_bounded opens its cursor with arraysize=FETCH_PAGE_SIZE so the connector's own
# FetchResults round trips match the paged fetch. Connection.cursor takes real keyword
# parameters (no **kwargs catch-all), so a rename would raise - this pin keeps that visible.
def test_connection_cursor_accepts_arraysize():
    params = inspect.signature(databricks_sql_client.Connection.cursor).parameters
    assert "arraysize" in params
    assert "kwargs" not in params
