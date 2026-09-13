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
