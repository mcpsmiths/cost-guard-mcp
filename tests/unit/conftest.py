"""Shared unit-test fakes and fixtures.

`FakeSnowflakeCursor` answers each statement the snowflake engine issues from fixture data
(the EXPLAIN plan, SHOW WAREHOUSES rows, the CURRENT_WAREHOUSE()/CURRENT_REGION() scalars,
QUERY_HISTORY rows) instead of returning one fetchone() value for every call. That lets a
test describe the warehouse a query is pinned to independently of the plan - which is what
the observed-warehouse pricing tests need - while staying a plain in-memory object: no
network, no connector import.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.config import (
    DATABRICKS_ALLOW_WAREHOUSE_START_ENV_VAR,
    SNOWFLAKE_ALLOW_WAREHOUSE_RESUME_ENV_VAR,
)


@pytest.fixture(autouse=True)
def _warehouse_state_guards_enforced(monkeypatch):
    """The read-only warehouse-state guards (DECISIONS.md #20) are read from the environment at
    call time; unset both opt-ins so the suite runs with the guards enforced regardless of the
    developer's shell. Opt-in tests set `monkeypatch.setenv(NAME, "true")` themselves."""
    monkeypatch.delenv(DATABRICKS_ALLOW_WAREHOUSE_START_ENV_VAR, raising=False)
    monkeypatch.delenv(SNOWFLAKE_ALLOW_WAREHOUSE_RESUME_ENV_VAR, raising=False)


# Column order of a SHOW WAREHOUSES result as the engine maps it by cursor.description
# name; the real command returns many more columns, in a different order - the engine must
# never rely on positions, only on these names.
SHOW_WAREHOUSES_COLUMNS = ("name", "state", "type", "size", "auto_resume", "generation")

_DEFAULT_PLAN = {
    "GlobalStats": {"partitionsTotal": 1, "partitionsAssigned": 1, "bytesAssigned": 1000}
}


def show_warehouses_row(
    name,
    size,
    *,
    warehouse_type="STANDARD",
    generation="2",
    state="STARTED",
    auto_resume="true",
):
    """One SHOW WAREHOUSES row in SHOW_WAREHOUSES_COLUMNS order, using the real text shapes
    Snowflake returns ('4X-Large', 'STANDARD', 'STARTED', 'true', '2')."""
    return (name, state, warehouse_type, size, auto_resume, generation)


class FakeSnowflakeCursor:
    def __init__(
        self,
        *,
        plan=_DEFAULT_PLAN,
        show_rows=(),
        region="AWS_US_EAST_1",
        current_warehouse=None,
        history_rows=(),
        fail_on=(),
    ):
        self.plan = plan
        self.show_rows = list(show_rows)
        self.region = region
        self.current_warehouse = current_warehouse
        self.history_rows = list(history_rows)
        self.fail_on = tuple(fail_on)
        self.executed: list[str] = []
        self.timeouts: list[float | None] = []
        self.statement_counts: list[int | None] = []
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, timeout=None, num_statements=None):
        for needle in self.fail_on:
            if needle in sql:
                raise RuntimeError(f"fake failure on {needle}")
        self.executed.append(sql)
        self.timeouts.append(timeout)
        # The engine forwards Snowflake's per-call statement-count pin (num_statements=1 on
        # model-supplied SQL); recorded so a test can assert it travelled with the statement.
        self.statement_counts.append(num_statements)
        self._last = sql

    @property
    def description(self):
        if self._last.startswith("SHOW WAREHOUSES"):
            return [
                (column, None, None, None, None, None, None) for column in SHOW_WAREHOUSES_COLUMNS
            ]
        return None

    def fetchone(self):
        if "EXPLAIN USING JSON" in self._last:
            return None if self.plan is None else (json.dumps(self.plan),)
        if "CURRENT_WAREHOUSE()" in self._last:
            return (self.current_warehouse,)
        if "CURRENT_REGION()" in self._last:
            if isinstance(self.region, tuple):
                return self.region  # a pre-built row, e.g. (None,) for a NULL region
            return None if self.region is None else (self.region,)
        return None

    def fetchall(self):
        if self._last.startswith("SHOW WAREHOUSES"):
            return list(self.show_rows)
        if "QUERY_HISTORY" in self._last:
            return list(self.history_rows)
        return []


def fake_snowflake_connection(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


@pytest.fixture
def snowflake_fakes():
    return SimpleNamespace(
        cursor=FakeSnowflakeCursor,
        connection=fake_snowflake_connection,
        show_row=show_warehouses_row,
        columns=SHOW_WAREHOUSES_COLUMNS,
    )


@pytest.fixture
def snowflake_config():
    """Patch the snowflake engine's load_snowflake_config with a fake standard-edition config.

    explain_estimate reads SnowflakeConfig.edition (SNOWFLAKE_EDITION) before it connects, so
    any test that drives explain_estimate with a patched _connect needs this too; tests that
    price at another edition set `snowflake_config.return_value.edition`.
    """
    with patch("cost_guard_mcp.engines.snowflake.load_snowflake_config") as mock_load_config:
        mock_load_config.return_value = MagicMock(
            account="abc123",
            user="svc_user",
            role="COST_GUARD_READER",
            private_key_path=None,
            private_key_passphrase=None,
            password=None,
            edition="standard",
        )
        yield mock_load_config
