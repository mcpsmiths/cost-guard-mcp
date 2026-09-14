# Databricks Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Databricks as a third supported engine (alongside BigQuery and Snowflake), wired into all four existing tools (`check_credentials`, `describe_engine_capabilities`, `estimate_query_cost`, `run_query_bounded`), with a `HEURISTIC`-tier cost estimate and a bounded/cancellable execution path — closing the one real remaining product gap in cost-guard-mcp.

**Architecture:** Mirrors the Snowflake engine's shape exactly: a `DatabricksConfig` loader in `config.py`, a `_connect()` helper + `check_credentials()`/`explain_estimate()`/`execute_bounded()` trio in `engines/databricks.py`, a pricing table in `pricing/databricks_pricing.py`, and dispatch branches added to the four existing `tools/*.py` files. Every function that calls the Databricks client library is wrapped with `@sanitize_exceptions("databricks")`, matching the non-negotiable rule already enforced for BigQuery and Snowflake.

**Tech Stack:** `databricks-sql-connector` (PyPI, `from databricks import sql`) for the DB-API connection; `databricks-sdk` (PyPI) for OAuth M2M service-principal auth only — everything else matches the existing stack (Python 3.12+, `uv`, `pytest`, `ruff`, `mypy`).

**Spec:** No separate spec document — this plan is grounded directly in three adversarially-verified `/deep-research` rounds run in-session on 2026-09-13/14 (connector API semantics, cost-estimation signal availability, pricing-model validation against Databricks' own official pricing page). Citations and confidence levels are inlined into the Global Constraints and task bodies below.

## Global Constraints

- **Accuracy tier is ALWAYS `HEURISTIC`, never `PRECISE` or `UPPER_BOUND`.** Confirmed via research: Databricks has no BigQuery-style dry-run anywhere in the product (re-verified against Databricks' own community forum and docs, 2026-09-13/14). `EXPLAIN COST` is the only pre-execution signal, and its plan-node statistics are frequently absent (no `ANALYZE TABLE`, streaming sources, non-Delta external tables).
- **`EXPLAIN COST` output is plain TEXT, not JSON** (unlike Snowflake's `EXPLAIN USING JSON`). Real confirmed format (docs.databricks.com/aws/en/optimizations/cbo, live-fetched 2026-09-13): `Relation[...] parquet, Statistics(sizeInBytes=134.6 GB, rowCount=2.88E+9, hints=none)`. Sizes carry a **unit suffix** (B/KB/MB/GB/TB/PB) requiring conversion — not a raw integer like Snowflake's `bytesAssigned`. Row counts may use scientific notation.
- **No system-table fallback for v1.** `system.query.history` and `system.billing.usage` are Unity-Catalog-governed and **admin-gated by default** — confirmed via 6+ independent official docs sources: a normal read-only service principal has zero access without an admin explicitly granting `USE CATALOG`/`USE SCHEMA`/`SELECT` on the system schema. A "look up historical runtime for similar queries" heuristic is explicitly out of scope — it is not a zero-friction v1 fallback.
- **Pricing scoped to Serverless SQL warehouses only.** Confirmed (Databricks' own pricing page + Azure Databricks pricing widget, live-fetched 2026-09-13/14): the DBU-per-hour table is identical across Classic/Pro/Serverless, but the **$/DBU dollar rate differs by tier** (Serverless $0.70/DBU US, Pro $0.55/DBU, Classic $0.22/DBU) — and Classic/Pro additionally require a **separate, second cloud-VM cost stream** (real AWS EC2 instance billing on top of the DBU charge) that this research round could not confirm against a primary AWS source. Modeling Classic/Pro correctly would require unverified guesswork; Serverless has one confirmed, self-contained rate. Classic/Pro support is explicitly deferred, documented as a known limitation exactly like Snowflake's own Gen1/US-East-only caveat.
- **No per-call warehouse override — this is a real architectural difference from Snowflake.** Databricks SQL has no `USE WAREHOUSE` statement; the SQL warehouse is fixed by the `http_path` used at connection time (an env-var-level, not a per-tool-call, setting). The `warehouse` parameter that `estimate_query_cost`/`run_query_bounded`/`check_credentials` already accept (for calling-convention consistency with the other two engines) is accepted but **ignored** for Databricks, with a code comment explaining why — never silently pretend it does something.
- **Dual auth, mirroring Snowflake's key-pair-preferred/password-fallback shape:** PAT (`access_token`, simple, no extra dependency) or OAuth M2M (`client_id`/`client_secret`, via the separate `databricks-sdk` package — confirmed NOT bundled with `databricks-sql-connector`, must be added as its own dependency). Env var names `DATABRICKS_CLIENT_ID`/`DATABRICKS_CLIENT_SECRET` match Databricks' own official code sample verbatim.
- **Do not pass `use_kernel=True` to `connect()`.** Confirmed via research: the newer Rust "kernel" backend explicitly rejects the generic `credentials_provider` OAuth M2M path with `NotSupportedError`. The default (legacy Thrift) backend supports both PAT and the generic `credentials_provider` OAuth M2M path — use the default, do not opt into the kernel backend.
- **Set `_socket_timeout` explicitly — never rely on the library's own default.** Confirmed real parameter (`CONNECTION_PARAMETERS.md`, verified directly against v4.5.0 shipped source): defaults to 900s on the Thrift backend used here. Not infinite, but still too long to leave implicit — matches this project's own established discipline (Snowflake `_LOGIN_TIMEOUT_SECONDS`/`_NETWORK_TIMEOUT_SECONDS`, PR #18) of always setting timeouts explicitly rather than trusting a client library's default, even a finite one.
- **No native async/polling execute mode exists.** Confirmed: `databricks-sql-connector` has no `execute_async`/`get_query_status` equivalent to Snowflake's. To bound `execute_bounded`'s wall-clock wait (matching the established 120s convention from BigQuery/Snowflake, PR #22), run `cursor.execute()` in a background thread, `join(timeout=120)`, and call the documented `Cursor.cancel()` — confirmed callable from a different thread than the one that called `execute()` per the connector's own source docstring — if the thread is still alive after the join times out.
- **`@sanitize_exceptions("databricks")` on every function that touches the client library** — non-negotiable, matching BigQuery/Snowflake, even though this specific connector's exceptions were confirmed NOT to embed credentials in their string representation (unlike the historical Snowflake issue #1323 this whole pattern was built to guard against) — defense-in-depth and consistency, not because this connector is known to leak.
- Row-cap wrapping uses the same `SELECT * FROM (...) AS cost_guard_row_cap LIMIT N+1` pattern already used for BigQuery/Snowflake — Databricks SQL (Spark SQL) supports `LIMIT` on a subquery identically.
- Coverage ≥80% (existing project rule); mock the Databricks client library in `tests/unit/` — never a real network call there.
- Keep files under 500 lines; one clear responsibility per file (existing project rule).

---

## File Structure

```
cost-guard-mcp/
├── pyproject.toml                              # add databricks-sql-connector, databricks-sdk (Phase 8)
├── README.md                                   # Databricks setup + tool list + known limitations (Phase 8)
├── src/cost_guard_mcp/
│   ├── config.py                               # add DatabricksConfig + load_databricks_config() (Phase 2)
│   ├── engines/
│   │   └── databricks.py                       # NEW: _connect, check_credentials, explain_estimate, execute_bounded (Phases 3-6)
│   ├── pricing/
│   │   └── databricks_pricing.py               # NEW: DBU/hour table + serverless $/DBU rate (Phase 1)
│   └── tools/
│       ├── check_credentials.py                # add databricks dispatch (Phase 7)
│       ├── describe_engine_capabilities.py      # add databricks capabilities entry (Phase 7)
│       ├── estimate_query_cost.py               # add databricks dispatch (Phase 7)
│       └── run_query_bounded.py                 # add databricks dispatch (Phase 7)
└── tests/unit/
    ├── test_databricks_pricing.py               # NEW (Phase 1)
    ├── test_config.py                           # add DatabricksConfig tests (Phase 2)
    ├── test_engines_databricks.py                # NEW (Phases 3-6)
    ├── test_tools_check_credentials.py           # add databricks dispatch test (Phase 7)
    ├── test_tools_describe_engine_capabilities.py # add databricks capabilities test (Phase 7)
    ├── test_tools_estimate_query_cost.py          # add databricks dispatch test (Phase 7)
    └── test_tools_run_query_bounded.py            # add databricks dispatch test (Phase 7)
```

`types.py` needs **no change** — `Engine = Literal["bigquery", "snowflake", "databricks"]` already includes `"databricks"` (added in the original v1 type design, deferred rather than implemented).

## Phase Overview

| Phase | Delivers |
|---|---|
| 1 | Databricks pricing table (DBU/hour by size, Serverless $/DBU rate) |
| 2 | `DatabricksConfig` + `load_databricks_config()` — dual auth, no dangerous defaults |
| 3 | `_connect()` — dual auth wiring, explicit socket timeout, default (Thrift) backend |
| 4 | `check_credentials()` — lightweight connectivity check, failure-as-data |
| 5 | `explain_estimate()` — `EXPLAIN COST` text parsing, HEURISTIC estimate, honest caveats |
| 6 | `execute_bounded()` — thread + timeout + cancel, row-cap wrapping |
| 7 | Wire into all 4 existing tools (dispatch + capabilities) |
| 8 | Dependencies, README, full-suite verification |

---

## Phase 1: Databricks pricing table

### Task 1.1: DBU/hour-by-size table and Serverless $/DBU rate

**Files:**
- Create: `src/cost_guard_mcp/pricing/databricks_pricing.py`
- Test: `tests/unit/test_databricks_pricing.py`

**Interfaces:**
- Produces: `dbus_per_hour(warehouse_size: str) -> float`, `SERVERLESS_USD_PER_DBU: float`. Consumed by `engines/databricks.py` (Phase 5).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_databricks_pricing.py
import pytest

from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU, dbus_per_hour


def test_dbus_per_hour_matches_confirmed_table():
    # Confirmed 2026-09-14 against Databricks' own pricing page + the Azure Databricks
    # pricing widget (identical DBU-count table across Classic/Pro/Serverless) —
    # https://www.databricks.com/product/pricing/databricks-lakehouse,
    # https://azure.microsoft.com/en-us/pricing/details/databricks
    assert dbus_per_hour("2X-Small") == 4
    assert dbus_per_hour("X-Small") == 6
    assert dbus_per_hour("Small") == 12
    assert dbus_per_hour("Medium") == 24
    assert dbus_per_hour("Large") == 40
    assert dbus_per_hour("X-Large") == 80
    assert dbus_per_hour("2X-Large") == 144
    assert dbus_per_hour("3X-Large") == 272
    assert dbus_per_hour("4X-Large") == 528


def test_dbus_per_hour_is_case_insensitive():
    assert dbus_per_hour("small") == 12
    assert dbus_per_hour("SMALL") == 12


def test_dbus_per_hour_rejects_unknown_size():
    # 5X-Large exists as a cluster size (Public Preview) but has no published DBU rate in
    # the confirmed pricing table — refuse rather than guess a number.
    with pytest.raises(ValueError, match="unknown Databricks warehouse size"):
        dbus_per_hour("5X-Large")


def test_serverless_rate_matches_confirmed_pricing_page():
    # $0.70/DBU, US region, confirmed live 2026-09-14 against databricks.com's own
    # pricing page (footnote "**Includes cloud instance cost" appears only on
    # Serverless — Classic $0.22/DBU and Pro $0.55/DBU both need a SEPARATE, unconfirmed
    # cloud-VM cost stream on top, which is why this project scopes to Serverless only).
    assert SERVERLESS_USD_PER_DBU == 0.70
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_databricks_pricing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cost_guard_mcp.pricing.databricks_pricing'`

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/pricing/databricks_pricing.py
"""Databricks Serverless SQL warehouse pricing.

Source: https://www.databricks.com/product/pricing/databricks-lakehouse (DBU-per-hour
table cross-checked against https://azure.microsoft.com/en-us/pricing/details/databricks,
both confirmed identical across Classic/Pro/Serverless — only the $/DBU dollar rate
differs by tier). Live-verified 2026-09-14. Re-fetch and update before each release;
Databricks has changed these before.

LIMITATION (documented, not fixed in v1): only Serverless SQL warehouse pricing is
modeled. Classic and Pro warehouses use lower DBU rates ($0.22 and $0.55 respectively,
vs. Serverless's $0.70) but require a SEPARATE, real AWS EC2 cost on top of the DBU
charge (Serverless bundles compute into its single rate; Classic/Pro do not) — that
second cost stream could not be confirmed against a primary AWS source and is out of
scope. Extend this module if Classic/Pro support becomes a priority.
"""

_DBUS_PER_HOUR: dict[str, float] = {
    "2X-SMALL": 4,
    "X-SMALL": 6,
    "SMALL": 12,
    "MEDIUM": 24,
    "LARGE": 40,
    "X-LARGE": 80,
    "2X-LARGE": 144,
    "3X-LARGE": 272,
    "4X-LARGE": 528,
}

# US region, AWS, Serverless SQL, pay-as-you-go. EU regions (e.g. Frankfurt) run higher
# (~$0.91/DBU) — not modeled here; this project assumes US-region pricing throughout,
# matching the same assumption already made for BigQuery and Snowflake pricing.
SERVERLESS_USD_PER_DBU = 0.70


def dbus_per_hour(warehouse_size: str) -> float:
    size = warehouse_size.upper()
    if size not in _DBUS_PER_HOUR:
        raise ValueError(
            f"unknown Databricks warehouse size '{warehouse_size}'. "
            f"Known sizes: {sorted(_DBUS_PER_HOUR)}."
        )
    return _DBUS_PER_HOUR[size]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_databricks_pricing.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cost_guard_mcp/pricing/databricks_pricing.py tests/unit/test_databricks_pricing.py
git commit -m "feat: add Databricks Serverless SQL DBU-rate pricing table"
```

---

## Phase 2: Config loader

### Task 2.1: `DatabricksConfig` + `load_databricks_config()`

**Files:**
- Modify: `src/cost_guard_mcp/config.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `DatabricksConfig(server_hostname, http_path, access_token, client_id, client_secret)`, `load_databricks_config() -> DatabricksConfig`. Consumed by `engines/databricks.py` (Phase 3).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_config.py — append
from cost_guard_mcp.config import load_databricks_config


def test_load_databricks_config_requires_server_hostname(monkeypatch):
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME", raising=False)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi_test")
    with pytest.raises(ConfigError, match="DATABRICKS_SERVER_HOSTNAME"):
        load_databricks_config()


def test_load_databricks_config_requires_http_path(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.delenv("DATABRICKS_HTTP_PATH", raising=False)
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi_test")
    with pytest.raises(ConfigError, match="DATABRICKS_HTTP_PATH"):
        load_databricks_config()


def test_load_databricks_config_requires_pat_or_oauth_m2m(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="DATABRICKS_TOKEN"):
        load_databricks_config()


def test_load_databricks_config_requires_both_oauth_fields_together(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-abc")
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="DATABRICKS_CLIENT_SECRET"):
        load_databricks_config()


def test_load_databricks_config_succeeds_with_pat(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi_test")
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    config = load_databricks_config()
    assert config.server_hostname == "my-workspace.cloud.databricks.com"
    assert config.http_path == "/sql/1.0/warehouses/abc123"
    assert config.access_token == "dapi_test"
    assert config.client_id is None
    assert config.client_secret is None


def test_load_databricks_config_succeeds_with_oauth_m2m(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "my-workspace.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-abc")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret-xyz")
    config = load_databricks_config()
    assert config.access_token is None
    assert config.client_id == "client-abc"
    assert config.client_secret == "secret-xyz"
```

The file's existing import block is:

```python
from cost_guard_mcp.config import (
    ConfigError,
    SnowflakeConfig,
    load_bigquery_config,
    load_snowflake_config,
)
```

Add `load_databricks_config` to that parenthesized import list — `pytest` and `ConfigError` are already imported, nothing else needs adding.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_databricks_config'`

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/config.py — add dataclass and loader function


@dataclass(frozen=True)
class DatabricksConfig:
    server_hostname: str
    http_path: str
    access_token: str | None
    client_id: str | None
    client_secret: str | None = field(repr=False)


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
```

Note: `field` is already imported at the top of `config.py` (used by `SnowflakeConfig`) — no new import needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: PASS (all config tests, including the 6 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/cost_guard_mcp/config.py tests/unit/test_config.py
git commit -m "feat: add Databricks config loader with PAT/OAuth-M2M dual auth"
```

---

## Phase 3: Connection helper

### Task 3.1: `_connect()` with dual auth and explicit socket timeout

**Files:**
- Create: `src/cost_guard_mcp/engines/databricks.py`
- Test: `tests/unit/test_engines_databricks.py`

**Interfaces:**
- Consumes: `load_databricks_config` (Phase 2), `sanitize_exceptions` (existing `errors.py`).
- Produces: `_connect() -> "databricks.sql.client.Connection"` (internal helper). Consumed by Phases 4-6.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_engines_databricks.py
from unittest.mock import MagicMock, patch

from cost_guard_mcp.engines.databricks import _connect


@patch("cost_guard_mcp.engines.databricks.sql.connect")
@patch("cost_guard_mcp.engines.databricks.load_databricks_config")
def test_connect_uses_access_token_when_pat_set(mock_load_config, mock_connect):
    mock_load_config.return_value = MagicMock(
        server_hostname="my-workspace.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        access_token="dapi_test",
        client_id=None,
        client_secret=None,
    )

    _connect()

    mock_connect.assert_called_once_with(
        server_hostname="my-workspace.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        access_token="dapi_test",
        _socket_timeout=60,
    )


@patch("cost_guard_mcp.engines.databricks.oauth_service_principal")
@patch("cost_guard_mcp.engines.databricks.Config")
@patch("cost_guard_mcp.engines.databricks.sql.connect")
@patch("cost_guard_mcp.engines.databricks.load_databricks_config")
def test_connect_uses_oauth_m2m_when_client_credentials_set(
    mock_load_config, mock_connect, mock_config_cls, mock_oauth_service_principal
):
    mock_load_config.return_value = MagicMock(
        server_hostname="my-workspace.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/abc123",
        access_token=None,
        client_id="client-abc",
        client_secret="secret-xyz",
    )
    mock_sdk_config = MagicMock()
    mock_config_cls.return_value = mock_sdk_config

    _connect()

    mock_config_cls.assert_called_once_with(
        host="https://my-workspace.cloud.databricks.com",
        client_id="client-abc",
        client_secret="secret-xyz",
    )
    _, kwargs = mock_connect.call_args
    assert kwargs["server_hostname"] == "my-workspace.cloud.databricks.com"
    assert kwargs["http_path"] == "/sql/1.0/warehouses/abc123"
    assert kwargs["_socket_timeout"] == 60
    assert callable(kwargs["credentials_provider"])
    kwargs["credentials_provider"]()
    mock_oauth_service_principal.assert_called_once_with(mock_sdk_config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_engines_databricks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cost_guard_mcp.engines.databricks'`

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/engines/databricks.py
from databricks import sql
from databricks.sdk.core import Config, oauth_service_principal

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

        def credentials_provider():
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_engines_databricks.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Verification spike — confirm `_socket_timeout` and `credentials_provider` against the actually-installed package**

Once Phase 8 adds the real dependency, before trusting this further:

```bash
uv run python -c "
import inspect
from databricks import sql
print(inspect.signature(sql.connect))
"
```
Expected: `credentials_provider` and `_socket_timeout` (or equivalent) appear as accepted parameters. If the installed version's signature differs from what research found, update `_connect()` accordingly — same discipline this project used for Snowflake's `login_timeout`/`network_timeout` (verified against installed source, not just docs).

- [ ] **Step 6: Commit**

```bash
git add src/cost_guard_mcp/engines/databricks.py tests/unit/test_engines_databricks.py
git commit -m "feat: add Databricks connection helper with dual PAT/OAuth-M2M auth"
```

---

## Phase 4: `check_credentials()`

### Task 4.1: Lightweight connectivity check, failure-as-data

**Files:**
- Modify: `src/cost_guard_mcp/engines/databricks.py`
- Modify: `tests/unit/test_engines_databricks.py`

**Interfaces:**
- Consumes: `_connect` (Phase 3), `redact_secrets` (existing `errors.py`).
- Produces: `check_credentials(warehouse: str | None = None) -> CredentialCheckResult`. Consumed by `tools/check_credentials.py` (Phase 7).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_engines_databricks.py — append
from cost_guard_mcp.engines.databricks import check_credentials


@patch("cost_guard_mcp.engines.databricks._connect")
def test_check_credentials_ok_when_query_succeeds(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("alice@example.com",)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials()

    mock_cursor.execute.assert_called_once_with("SELECT current_user()")
    assert result.engine == "databricks"
    assert result.ok is True
    assert "alice@example.com" in result.detail


@patch("cost_guard_mcp.engines.databricks._connect")
def test_check_credentials_reports_failure_as_data_not_a_raise(mock_connect):
    mock_connect.side_effect = Exception("401 Unauthorized")

    result = check_credentials()

    assert result.engine == "databricks"
    assert result.ok is False
    assert "401 Unauthorized" in result.detail


@patch("cost_guard_mcp.engines.databricks._connect")
def test_check_credentials_ignores_warehouse_parameter(mock_connect):
    # Databricks has no per-query USE WAREHOUSE - the warehouse parameter exists only
    # for calling-convention consistency with bigquery/snowflake and is a documented
    # no-op here.
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("alice@example.com",)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    result = check_credentials(warehouse="some-warehouse-name")

    assert result.ok is True
    mock_cursor.execute.assert_called_once_with("SELECT current_user()")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_engines_databricks.py -v`
Expected: FAIL — `check_credentials` not defined.

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/engines/databricks.py — append
from cost_guard_mcp.errors import redact_secrets
from cost_guard_mcp.types import CredentialCheckResult


def check_credentials(warehouse: str | None = None) -> CredentialCheckResult:
    """Verify Databricks credentials/connectivity without running EXPLAIN COST or any real
    query.

    `warehouse` is accepted only for calling-convention consistency with the bigquery/
    snowflake engines and is a documented no-op: Databricks has no per-query USE WAREHOUSE
    equivalent — the SQL warehouse is fixed by DATABRICKS_HTTP_PATH at connect time.

    Deliberately does NOT use @sanitize_exceptions: a failed check is the expected, useful
    result here, not an error condition — this function always returns a
    CredentialCheckResult so a caller can distinguish "not configured yet" from "actually
    broken" before attempting a real estimate_query_cost/run_query_bounded call.
    """
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT current_user()")
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("SELECT current_user() returned no rows")
            current_user = row[0]
    except Exception as exc:  # noqa: BLE001 - reporting failure as data, not raising, by design
        return CredentialCheckResult(engine="databricks", ok=False, detail=redact_secrets(str(exc)))

    return CredentialCheckResult(
        engine="databricks", ok=True, detail=f"Authenticated as '{current_user}'."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_engines_databricks.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cost_guard_mcp/engines/databricks.py tests/unit/test_engines_databricks.py
git commit -m "feat: add Databricks check_credentials with documented warehouse no-op"
```

---

## Phase 5: `explain_estimate()` — HEURISTIC cost estimate

### Task 5.1: `EXPLAIN COST` text parsing and flat-runtime-assumption dollar estimate

**Files:**
- Modify: `src/cost_guard_mcp/engines/databricks.py`
- Modify: `tests/unit/test_engines_databricks.py`

**Interfaces:**
- Consumes: `_connect` (Phase 3), `dbus_per_hour`/`SERVERLESS_USD_PER_DBU` (Phase 1), `CostEstimate`/`AccuracyTier` (existing `types.py`).
- Produces: `explain_estimate(sql_text: str, warehouse: str | None, warehouse_size: str = "X-Small") -> CostEstimate`. Consumed by `tools/estimate_query_cost.py` (Phase 7).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_engines_databricks.py — append
from cost_guard_mcp.engines.databricks import explain_estimate
from cost_guard_mcp.types import AccuracyTier


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_parses_max_size_in_bytes_from_statistics(mock_connect):
    # Real confirmed EXPLAIN COST output shape (docs.databricks.com/aws/en/optimizations/cbo,
    # live-verified 2026-09-13): one Statistics(sizeInBytes=X unit, rowCount=Y) tuple per
    # plan node, larger unit suffixes (GB) alongside smaller ones (B) in the same plan.
    explain_output = (
        "== Optimized Logical Plan ==\n"
        "Aggregate [count(1) AS count(1)#2L], Statistics(sizeInBytes=20.0 B, rowCount=1)\n"
        "+- Relation[ss_store_sk,ss_sold_date_sk] parquet, "
        "Statistics(sizeInBytes=134.6 GB, rowCount=2.88E+9, hints=none)\n"
    )
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(explain_output,)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT COUNT(*) FROM store_sales", warehouse=None)

    executed_sql = mock_cursor.execute.call_args[0][0]
    assert executed_sql == "EXPLAIN COST SELECT COUNT(*) FROM store_sales"
    assert estimate.engine == "databricks"
    assert estimate.accuracy_tier == AccuracyTier.HEURISTIC
    assert estimate.estimated_bytes == int(134.6 * 1024**3)
    assert estimate.estimated_cost_usd is not None


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_handles_missing_statistics(mock_connect):
    # Real, confirmed case: no ANALYZE TABLE run, streaming source, or non-Delta external
    # table with no collected stats.
    explain_output = "== Optimized Logical Plan ==\nRelation[a,b] csv\n"
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(explain_output,)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT * FROM external_csv_table", warehouse=None)

    assert estimate.estimated_bytes is None
    assert estimate.accuracy_tier == AccuracyTier.HEURISTIC
    assert any("no size statistics" in c.lower() for c in estimate.caveats)


@patch("cost_guard_mcp.engines.databricks._connect")
def test_explain_estimate_cost_math_matches_pricing_table(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("Relation[a] parquet, Statistics(sizeInBytes=1.0 B)",)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    estimate = explain_estimate("SELECT 1", warehouse=None, warehouse_size="Small")

    from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU, dbus_per_hour

    expected_cost = round(dbus_per_hour("Small") * SERVERLESS_USD_PER_DBU * (30 / 3600), 6)
    assert estimate.estimated_cost_usd == expected_cost
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_engines_databricks.py -v`
Expected: FAIL — `explain_estimate` not defined.

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/engines/databricks.py — append
import re

from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU, dbus_per_hour
from cost_guard_mcp.types import AccuracyTier, CostEstimate

_UNIT_MULTIPLIERS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}

# Real confirmed EXPLAIN COST output format (docs.databricks.com/aws/en/optimizations/cbo,
# live-verified 2026-09-13): "Statistics(sizeInBytes=134.6 GB, rowCount=2.88E+9, ...)" -
# unit-suffixed, not a raw byte integer like Snowflake's bytesAssigned.
_STATISTICS_RE = re.compile(r"sizeInBytes=([\d.]+)\s*(B|KB|MB|GB|TB|PB)\b", re.IGNORECASE)

# EXPLAIN COST gives no runtime estimate at all - same deliberately conservative,
# documented placeholder pattern already used for Snowflake (see _ASSUMED_RUNTIME_HOURS
# in snowflake.py), turning a byte estimate (when available) into SOME dollar figure.
_ASSUMED_RUNTIME_HOURS = 30 / 3600


def _parse_max_size_in_bytes(explain_output: str) -> int | None:
    matches = _STATISTICS_RE.findall(explain_output)
    if not matches:
        return None
    sizes = [float(value) * _UNIT_MULTIPLIERS[unit.upper()] for value, unit in matches]
    return int(max(sizes))


@sanitize_exceptions("databricks")
def explain_estimate(
    sql_text: str, warehouse: str | None, warehouse_size: str = "X-Small"
) -> CostEstimate:
    """Estimate Databricks query cost via EXPLAIN COST. Always HEURISTIC - never PRECISE
    or UPPER_BOUND - since Databricks has no dry-run and EXPLAIN COST's plan-node
    statistics are frequently absent (no ANALYZE TABLE, streaming sources, non-Delta
    external tables).

    `warehouse` is accepted only for calling-convention consistency and is a documented
    no-op - see check_credentials's docstring for why.
    """
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN COST {sql_text}")
        rows = cur.fetchall()

    explain_output = "\n".join(row[0] for row in rows)
    max_size_in_bytes = _parse_max_size_in_bytes(explain_output)

    rate = dbus_per_hour(warehouse_size)
    estimated_cost_usd = rate * SERVERLESS_USD_PER_DBU * _ASSUMED_RUNTIME_HOURS

    caveats = [
        "Databricks has no BigQuery-style dry-run; this estimate is HEURISTIC, the "
        "least precise of this project's three accuracy tiers.",
        f"Cost assumes a {int(_ASSUMED_RUNTIME_HOURS * 3600)}-second runtime on a "
        f"{warehouse_size} Serverless SQL warehouse - a rough placeholder, not derived "
        "from this query's actual expected runtime.",
        "Pricing assumes a Serverless SQL warehouse; Classic/Pro warehouses use "
        "different (lower) DBU rates plus a separate underlying cloud VM cost not "
        "modeled here.",
    ]
    if max_size_in_bytes is None:
        caveats.append(
            "EXPLAIN COST returned no size statistics for this query (common without "
            "ANALYZE TABLE having been run, for streaming sources, or for non-Delta "
            "external tables) - the byte estimate is unavailable, not zero."
        )

    return CostEstimate(
        engine="databricks",
        accuracy_tier=AccuracyTier.HEURISTIC,
        estimated_bytes=max_size_in_bytes,
        estimated_cost_usd=round(estimated_cost_usd, 6),
        caveats=caveats,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_engines_databricks.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cost_guard_mcp/engines/databricks.py tests/unit/test_engines_databricks.py
git commit -m "feat: add Databricks EXPLAIN COST HEURISTIC cost estimate"
```

---

## Phase 6: `execute_bounded()` — thread + timeout + cancel

### Task 6.1: Bounded execution using a background thread and `Cursor.cancel()`

**Files:**
- Modify: `src/cost_guard_mcp/engines/databricks.py`
- Modify: `tests/unit/test_engines_databricks.py`

**Interfaces:**
- Consumes: `_connect` (Phase 3).
- Produces: `execute_bounded(sql_text: str, warehouse: str | None, max_rows: int | None) -> tuple[list[dict], int, bool]`. Consumed by `tools/run_query_bounded.py` (Phase 7).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_engines_databricks.py — append
import threading
import time

from cost_guard_mcp.engines.databricks import execute_bounded
from cost_guard_mcp.errors import SanitizedEngineError


def _make_mock_cursor(rows, execute_delay_seconds=0.0):
    mock_cursor = MagicMock()

    def _execute(_sql):
        if execute_delay_seconds:
            time.sleep(execute_delay_seconds)

    mock_cursor.execute.side_effect = _execute
    mock_cursor.fetchall.return_value = rows
    return mock_cursor


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_wraps_query_with_limit_when_max_rows_set(mock_connect):
    mock_cursor = _make_mock_cursor([{"a": 1}, {"a": 2}, {"a": 3}])
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    rows, row_count, row_cap_hit = execute_bounded("SELECT * FROM t", warehouse=None, max_rows=2)

    called_sql = mock_cursor.execute.call_args[0][0]
    assert "LIMIT 3" in called_sql  # max_rows + 1
    assert row_count == 2
    assert row_cap_hit is True
    assert rows == [{"a": 1}, {"a": 2}]


@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_no_cap_hit_when_fewer_rows_than_max(mock_connect):
    mock_cursor = _make_mock_cursor([{"a": 1}])
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    rows, row_count, row_cap_hit = execute_bounded("SELECT * FROM t", warehouse=None, max_rows=5)

    assert row_count == 1
    assert row_cap_hit is False


@patch(
    'cost_guard_mcp.engines.databricks.execute_bounded.__wrapped__.__globals__["_MAX_EXECUTION_WAIT_SECONDS"]',
    0.2,
)
@patch("cost_guard_mcp.engines.databricks._connect")
def test_execute_bounded_cancels_and_raises_when_wait_times_out(mock_connect):
    mock_cursor = _make_mock_cursor([], execute_delay_seconds=2.0)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    with pytest.raises(SanitizedEngineError, match="Query exceeded"):
        execute_bounded("SELECT * FROM huge_table", warehouse=None, max_rows=None)

    mock_cursor.cancel.assert_called_once()
```

Note: the `@patch(...__wrapped__.__globals__[...])` line above is a placeholder pattern for overriding the module-level timeout constant in a single test — **before writing Task 6.1's implementation**, check whether `@sanitize_exceptions` preserves `__wrapped__` (it uses `functools.wraps`, confirmed in `errors.py`, so it does). If this exact patch target proves awkward in practice, the simpler alternative is to add a `_max_wait_seconds: float | None = None` keyword-only parameter to `execute_bounded` defaulting to the module constant, and pass `_max_wait_seconds=0.2` directly from this test instead of patching a global — prefer that simpler approach when writing the real implementation and test if the patch-based version above is awkward to get working; either way the observable behavior under test (cancel called, `SanitizedEngineError` raised, message contains "Query exceeded") is what matters, not the exact mechanism for shortening the wait in the test.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_engines_databricks.py -v`
Expected: FAIL — `execute_bounded` not defined.

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/engines/databricks.py — append
import threading

# dust-tt/dust's precedent for bounding a long-running warehouse job, already applied to
# Snowflake in this project (PR #22): give up after 2 minutes. Unlike Snowflake, there is
# no native async/polling execute mode here (confirmed via research) - execute() runs in
# a background thread, joined with this timeout, and Cursor.cancel() is called if it is
# still alive. cancel() is confirmed callable from a different thread than the one that
# called execute(), per the connector's own source docstring.
_MAX_EXECUTION_WAIT_SECONDS = 120


@sanitize_exceptions("databricks")
def execute_bounded(
    sql_text: str, warehouse: str | None, max_rows: int | None
) -> tuple[list[dict], int, bool]:
    """Execute `sql_text` with an optional row bound and a wall-clock execution cap.

    `warehouse` is accepted only for calling-convention consistency and is a documented
    no-op - see check_credentials's docstring for why.
    """
    conn = _connect()
    wrapped_sql = sql_text
    if max_rows is not None:
        # `sql_text` is the caller's own query, passed as this tool's actual `sql`
        # parameter - wrapping their query in a LIMIT subquery to cap rows is this
        # function's job, not untrusted input reaching a query built elsewhere.
        wrapped_sql = f"SELECT * FROM ({sql_text}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

    cur = conn.cursor()
    execution_error: list[BaseException] = []

    def _run() -> None:
        try:
            cur.execute(wrapped_sql)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            execution_error.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=_MAX_EXECUTION_WAIT_SECONDS)

    if thread.is_alive():
        try:
            cur.cancel()
        except Exception:  # noqa: BLE001, S110 - best-effort; the TimeoutError below matters
            pass
        raise TimeoutError(f"Query exceeded {_MAX_EXECUTION_WAIT_SECONDS}s and was cancelled.")

    if execution_error:
        raise execution_error[0]

    rows = [dict(row) for row in cur.fetchall()]
    cur.close()

    row_cap_hit = max_rows is not None and len(rows) > max_rows
    if row_cap_hit:
        rows = rows[:max_rows]

    return rows, len(rows), row_cap_hit
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_engines_databricks.py -v`
Expected: PASS (11 passed). If the timeout test from Step 1 needs adjusting per its own note, do so now and re-run.

- [ ] **Step 5: Commit**

```bash
git add src/cost_guard_mcp/engines/databricks.py tests/unit/test_engines_databricks.py
git commit -m "feat: add Databricks bounded execution with thread+cancel timeout"
```

---

## Phase 7: Wire into the four existing tools

### Task 7.1: `describe_engine_capabilities` — add Databricks entry

**Files:**
- Modify: `src/cost_guard_mcp/tools/describe_engine_capabilities.py`
- Modify: `tests/unit/test_tools_describe_engine_capabilities.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_describe_engine_capabilities.py — append
def test_databricks_capabilities_are_heuristic_by_default():
    caps = describe_engine_capabilities("databricks")
    assert caps.engine == "databricks"
    assert caps.supports_precise_bytes is False
    assert caps.supports_dollar_estimate is True
    assert caps.default_accuracy_tier == AccuracyTier.HEURISTIC
    assert any("EXPLAIN COST" in gap for gap in caps.known_gaps)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_tools_describe_engine_capabilities.py -v`
Expected: FAIL — `describe_engine_capabilities("databricks")` currently raises `UserVisibleError` ("not yet supported").

- [ ] **Step 3: Add the entry**

```python
# src/cost_guard_mcp/tools/describe_engine_capabilities.py — add to _CAPABILITIES
    "databricks": EngineCapabilities(
        engine="databricks",
        supports_precise_bytes=False,
        supports_dollar_estimate=True,
        default_accuracy_tier=AccuracyTier.HEURISTIC,
        known_gaps=[
            "Databricks has no BigQuery-style dry-run; EXPLAIN COST's plan-node "
            "statistics are frequently absent (no ANALYZE TABLE run, streaming "
            "sources, non-Delta external tables) - byte estimates may be unavailable.",
            "The dollar figure assumes a fixed placeholder runtime on a Serverless SQL "
            "warehouse, not derived from this query's actual expected runtime.",
            "Only Serverless SQL warehouse pricing is modeled - Classic/Pro warehouses "
            "use different DBU rates plus a separate cloud VM cost not modeled here.",
            "There is no per-query USE WAREHOUSE equivalent - the SQL warehouse is "
            "fixed by DATABRICKS_HTTP_PATH at connect time, not overridable per call.",
        ],
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_tools_describe_engine_capabilities.py -v`
Expected: PASS

- [ ] **Step 5: Delete the now-broken, now-redundant `test_unsupported_engine_raises_clear_error`**

This existing test currently uses `"databricks"` as its unsupported-engine example (`with pytest.raises(ValueError, match="databricks"): describe_engine_capabilities("databricks")`) — it will now FAIL, since `"databricks"` is supported. Delete it rather than rename it: the same file's `test_unknown_engine_string_raises_clear_error` (using `"redshift"`) already covers the identical "unsupported engine" code path, so keeping both would be a duplicate test, not added coverage.

- [ ] **Step 6: Re-run the full file to confirm the suite is clean**

Run: `uv run pytest tests/unit/test_tools_describe_engine_capabilities.py -v`
Expected: PASS (3 tests: bigquery-default, unknown-engine/redshift, snowflake-default, databricks-default — 4 total after deletion, not 5)

- [ ] **Step 7: Commit**

```bash
git add src/cost_guard_mcp/tools/describe_engine_capabilities.py tests/unit/test_tools_describe_engine_capabilities.py
git commit -m "feat: add databricks entry to describe_engine_capabilities"
```

---

### Task 7.2: `estimate_query_cost` — add Databricks dispatch

**Files:**
- Modify: `src/cost_guard_mcp/tools/estimate_query_cost.py`
- Modify: `tests/unit/test_tools_estimate_query_cost.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_estimate_query_cost.py — append
@patch("cost_guard_mcp.tools.estimate_query_cost.databricks_engine")
def test_estimate_query_cost_dispatches_to_databricks(mock_engine):
    mock_engine.explain_estimate.return_value = CostEstimate(
        engine="databricks", accuracy_tier=AccuracyTier.HEURISTIC, estimated_bytes=100
    )
    result = estimate_query_cost("databricks", "SELECT 1", warehouse="ignored")
    mock_engine.explain_estimate.assert_called_once_with("SELECT 1", "ignored")
    assert result.engine == "databricks"
```

The existing `test_estimate_query_cost_rejects_unsupported_engine` test uses `"databricks"` as its unsupported-engine example (`with pytest.raises(ValueError, match="databricks"): estimate_query_cost("databricks", "SELECT 1")`) — confirmed via `grep -n 'match="databricks"' tests/unit/`. It will FAIL once this task lands. Update it now (this file has no separate "redshift"-style test to make it redundant, unlike Task 7.1's file — rename, don't delete):

```python
# tests/unit/test_tools_estimate_query_cost.py — replace the existing test
def test_estimate_query_cost_rejects_unsupported_engine():
    with pytest.raises(ValueError, match="redshift"):
        estimate_query_cost("redshift", "SELECT 1")  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_tools_estimate_query_cost.py -v`
Expected: FAIL — `AttributeError: module has no attribute 'databricks_engine'`.

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/tools/estimate_query_cost.py — replace the whole file
from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.types import CostEstimate, Engine


def estimate_query_cost(engine: Engine, sql: str, warehouse: str | None = None) -> CostEstimate:
    if engine == "bigquery":
        return bigquery_engine.dry_run(sql)
    if engine == "snowflake":
        return snowflake_engine.explain_estimate(sql, warehouse)
    if engine == "databricks":
        return databricks_engine.explain_estimate(sql, warehouse)
    raise UserVisibleError(
        f"estimate_query_cost: engine '{engine}' is not yet supported. "
        "Supported engines: ['bigquery', 'snowflake', 'databricks']."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_tools_estimate_query_cost.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cost_guard_mcp/tools/estimate_query_cost.py tests/unit/test_tools_estimate_query_cost.py
git commit -m "feat: wire estimate_query_cost tool for databricks"
```

---

### Task 7.3: `run_query_bounded` — add Databricks dispatch

**Files:**
- Modify: `src/cost_guard_mcp/tools/run_query_bounded.py`
- Modify: `tests/unit/test_tools_run_query_bounded.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_run_query_bounded.py — append
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_run_query_bounded_dispatches_to_databricks(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("databricks", "SELECT 1")

    mock_engine.execute_bounded.assert_called_once_with("SELECT 1", warehouse=None, max_rows=None)
    assert result.status == "ok"
```

(`_estimate` is the existing helper already defined at the top of this test file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_tools_run_query_bounded.py -v`
Expected: FAIL — `AttributeError: module has no attribute 'databricks_engine'`.

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/tools/run_query_bounded.py — modify imports and the engine dispatch block
from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.tools.estimate_query_cost import estimate_query_cost
from cost_guard_mcp.types import BoundedQueryResult, Engine, RefusalReason
```

Leave the rest of the function body unchanged up to the engine dispatch block, then replace just that block:

```python
    if engine == "bigquery":
        rows, row_count, row_cap_hit = bigquery_engine.execute_bounded(
            sql, max_bytes_billed=max_bytes_billed, max_rows=max_rows
        )
    elif engine == "snowflake":
        rows, row_count, row_cap_hit = snowflake_engine.execute_bounded(
            sql, warehouse=warehouse, max_rows=max_rows
        )
    elif engine == "databricks":
        rows, row_count, row_cap_hit = databricks_engine.execute_bounded(
            sql, warehouse=warehouse, max_rows=max_rows
        )
    else:
        raise UserVisibleError(f"run_query_bounded: engine '{engine}' is not yet supported.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_tools_run_query_bounded.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cost_guard_mcp/tools/run_query_bounded.py tests/unit/test_tools_run_query_bounded.py
git commit -m "feat: wire run_query_bounded tool for databricks"
```

---

### Task 7.4: `check_credentials` tool — add Databricks dispatch

**Files:**
- Modify: `src/cost_guard_mcp/tools/check_credentials.py`
- Modify: `tests/unit/test_tools_check_credentials.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_check_credentials.py — append
@patch("cost_guard_mcp.tools.check_credentials.databricks_engine")
def test_check_credentials_dispatches_to_databricks(mock_engine):
    mock_engine.check_credentials.return_value = CredentialCheckResult(
        engine="databricks", ok=True, detail="Authenticated."
    )
    result = check_credentials("databricks", warehouse="ignored")
    mock_engine.check_credentials.assert_called_once_with("ignored")
    assert result.engine == "databricks"
```

The existing `test_check_credentials_rejects_unsupported_engine` test uses `"databricks"` as its unsupported-engine example (confirmed via `grep -n 'match="databricks"' tests/unit/`) — it will FAIL once this task lands. Update it now:

```python
# tests/unit/test_tools_check_credentials.py — replace the existing test
def test_check_credentials_rejects_unsupported_engine():
    with pytest.raises(ValueError, match="redshift"):
        check_credentials("redshift")  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_tools_check_credentials.py -v`
Expected: FAIL — `AttributeError: module has no attribute 'databricks_engine'`.

- [ ] **Step 3: Write the implementation**

```python
# src/cost_guard_mcp/tools/check_credentials.py — replace the whole file
from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.types import CredentialCheckResult, Engine


def check_credentials(engine: Engine, warehouse: str | None = None) -> CredentialCheckResult:
    if engine == "bigquery":
        return bigquery_engine.check_credentials()
    if engine == "snowflake":
        return snowflake_engine.check_credentials(warehouse)
    if engine == "databricks":
        return databricks_engine.check_credentials(warehouse)
    raise UserVisibleError(
        f"check_credentials: engine '{engine}' is not yet supported. "
        "Supported engines: ['bigquery', 'snowflake', 'databricks']."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_tools_check_credentials.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cost_guard_mcp/tools/check_credentials.py tests/unit/test_tools_check_credentials.py
git commit -m "feat: wire check_credentials tool for databricks"
```

---

## Phase 8: Dependencies, README, full-suite verification

### Task 8.1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the two new dependencies**

```toml
# pyproject.toml — dependencies list
dependencies = [
    "mcp>=2.0.0",
    "google-cloud-bigquery>=3.25.0",
    "google-cloud-bigquery-reservation>=1.15.0",
    "snowflake-connector-python>=3.12.0",
    "databricks-sql-connector>=4.0.0",
    "databricks-sdk>=0.20.0",
    "pydantic>=2.9.0",
]
```

- [ ] **Step 2: Sync and confirm the real installed API surface matches what Phases 3-6 assumed**

```bash
uv sync --all-extras
uv run python -c "
from databricks import sql
from databricks.sdk.core import Config, oauth_service_principal
import inspect
print(inspect.signature(sql.connect))
print(inspect.signature(oauth_service_principal))
"
```

Expected: `credentials_provider` and `_socket_timeout` appear in `sql.connect`'s signature (or `**kwargs`, in which case check `CONNECTION_PARAMETERS.md` in the installed package's own repo/README for confirmation instead); `oauth_service_principal(cfg)` takes a single `Config` argument. If anything differs from what Phases 3-6 assumed, fix those files now, matching this project's own established discipline of verifying installed-package reality over trusting docs or research alone.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add databricks-sql-connector and databricks-sdk dependencies"
```

---

### Task 8.2: README updates

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a Databricks setup section** (after the existing Snowflake setup section)

```markdown
### Databricks
Set `DATABRICKS_SERVER_HOSTNAME` and `DATABRICKS_HTTP_PATH` (from the SQL warehouse's
Connection Details tab), and either `DATABRICKS_TOKEN` (a personal access token,
simplest) or `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` (OAuth machine-to-machine
via a service principal, preferred for automated use). Only Serverless SQL warehouses are
priced accurately — see Known Limitations.
```

- [ ] **Step 2: Update the Tools section's `estimate_query_cost`/`run_query_bounded`/`check_credentials` lines** to say "BigQuery, Snowflake, and Databricks" instead of just the first two, and add Databricks example env vars to `.mcp.json.example` and the Codex TOML block, mirroring the existing Snowflake entries.

- [ ] **Step 3: Update Known Limitations**

```markdown
- Databricks cost estimates are always `HEURISTIC` (the least precise tier) - Databricks
  has no dry-run, and `EXPLAIN COST`'s byte estimates are frequently unavailable.
- Databricks pricing only models Serverless SQL warehouses - Classic/Pro warehouses use
  different (lower) DBU rates plus a separate cloud VM cost not modeled here.
- Databricks has no per-query warehouse override - the SQL warehouse is fixed by
  `DATABRICKS_HTTP_PATH` at connect time.
```

- [ ] **Step 4: Commit**

```bash
git add README.md .mcp.json.example
git commit -m "docs: add Databricks setup, tool coverage, and known limitations"
```

---

### Task 8.3: Full-suite verification

- [ ] **Step 1: Run the complete check suite**

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest tests/unit -v --cov=cost_guard_mcp --cov-report=term-missing --cov-fail-under=80
```

Expected: all clean, ≥80% coverage (should be well above, matching the ~93-97% this project has maintained throughout).

- [ ] **Step 2: Manual end-to-end smoke test with the real official MCP client** (matching the precedent already established for the Dockerfile PR — use the SDK's own client, not hand-rolled JSON-RPC lines)

```bash
uv run python -c "
import asyncio
from mcp.client.stdio import stdio_client
from mcp.client.session import ClientSession
from mcp import StdioServerParameters

async def main():
    params = StdioServerParameters(command='uv', args=['run', 'cost-guard-mcp'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool('describe_engine_capabilities', {'engine': 'databricks'})
            print(result.structured_content)

asyncio.run(main())
"
```

Expected: prints the Databricks `EngineCapabilities` dict with `default_accuracy_tier: HEURISTIC`.

- [ ] **Step 3: If real Databricks credentials are available (a free-tier/trial workspace), run one live check** — not required to ship this plan, but do it if credentials are on hand, mirroring the live-integration discipline already established for BigQuery/Snowflake in this project's history:

```bash
export DATABRICKS_SERVER_HOSTNAME=<your-workspace>.cloud.databricks.com
export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>
export DATABRICKS_TOKEN=<your-pat>
uv run python -c "
from cost_guard_mcp.engines.databricks import check_credentials, explain_estimate
print(check_credentials())
print(explain_estimate('SELECT 1', warehouse=None))
"
```

- [ ] **Step 4: Final commit if any fixes were needed during verification, then follow this repo's established branch → PR → CI → merge workflow.**

## End-to-end verification checklist

1. All 8 phases' tests pass, coverage ≥80%.
2. `describe_engine_capabilities("databricks")` returns `HEURISTIC` tier with real known-gaps text.
3. `estimate_query_cost("databricks", sql)` returns a `CostEstimate` with `accuracy_tier=HEURISTIC`.
4. `run_query_bounded("databricks", sql, max_rows=N)` correctly caps rows and reports `row_cap_hit`.
5. `check_credentials("databricks")` reports failure as data (not a raised exception) when misconfigured.
6. A query that runs long enough is cancelled and raises a clear `TimeoutError`-derived message, not a silent hang.
7. Real official MCP client round-trip (Step 2 above) succeeds.
