import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import TypeGuard

import snowflake.connector

from cost_guard_mcp.config import load_snowflake_config
from cost_guard_mcp.errors import (
    SanitizedEngineError,
    present_remote_text,
    redact_secrets,
    sanitize_exceptions,
)
from cost_guard_mcp.pricing.runtime_scaling import scale_runtime_hours
from cost_guard_mcp.pricing.snowflake_pricing import credits_per_hour, usd_per_credit
from cost_guard_mcp.sql_scope import single_statement_body
from cost_guard_mcp.types import (
    AccuracyTier,
    CostEstimate,
    CredentialCheckResult,
    HistoricalRuntimeSignal,
)

logger = logging.getLogger(__name__)

# The connector's own defaults leave this tool exposed to indefinite hangs: login_timeout
# falls back to snowflake.connector.auth.by_plugin.DEFAULT_AUTH_CLASS_TIMEOUT (120s) only if
# unset, and network_timeout has NO fallback at all — it's infinite. A pre-flight cost-check
# tool that can hang forever on a stalled connection defeats its own purpose, so both are set
# explicitly here rather than left to those defaults.
_LOGIN_TIMEOUT_SECONDS = 30
_NETWORK_TIMEOUT_SECONDS = 60

# Security audit record snowflake-execute-unpinned-multi-statement-count: EXPLAIN USING JSON
# and execute_async both carry model-supplied text. Without num_statements the connector sends
# no MULTI_STATEMENT_COUNT, so whether a ';'-separated trailing statement executed - inside a
# tool advertised read_only_hint=True, and ahead of run_query_bounded's cap gate - depended on
# the operator's account/user MULTI_STATEMENT_COUNT parameter. Passing num_statements=1 makes
# Snowflake itself reject any text that is not exactly one statement ("Actual statement count
# N did not match the desired statement count 1"), regardless of that parameter; the same
# value also goes in the login-time session_parameters as belt and braces for any future
# execute added without the kwarg. tests/unit/test_snowflake_dependency_contract.py pins the
# connector API this relies on.
_SINGLE_STATEMENT_COUNT = 1

# Provider-side statement deadline (security audit record client-side-only-query-deadline-
# orphaned-on-process-exit, Snowflake leg; plan-review amendment A8). execute_bounded's
# poll/cancel watchdog and the connector's network_timeout are both client-side: if this
# process dies mid-call (host closes stdin, SIGTERM, OOM) the warehouse keeps running - and
# billing - the query until Snowflake's own STATEMENT_TIMEOUT_IN_SECONDS fires, whose shipped
# default is 172800 s (two days). A session-level value is therefore set so the deadline
# survives this process. It is applied read-then-only-lower, NOT as a blind login-time
# session parameter: a SESSION-level value OVERRIDES the account- and user-level values (only
# the warehouse level combines lowest-wins), so blindly setting 120 would RAISE an operator's
# stricter 30 s user policy. SHOW PARAMETERS ... IN SESSION is metadata-only (no warehouse)
# and bounded by the same short client-side timeout the other metadata probes use.
_SESSION_PARAMETER_PROBE_TIMEOUT_SECONDS = 5
_STATEMENT_TIMEOUT_PARAMETER = "STATEMENT_TIMEOUT_IN_SECONDS"


def _parse_effective_statement_timeout(row: tuple | None) -> int | None:
    """The `value` column of a SHOW PARAMETERS row as a positive int, else None.

    SHOW PARAMETERS rows are (key, value, default, level, description, type). 0 means "no
    timeout" in Snowflake - the LEAST bounded value, so it is reported as None (needs
    lowering), exactly like an unparseable or missing value.
    """
    if row is None or len(row) < 2:
        return None
    try:
        effective = int(str(row[1]).strip())
    except (TypeError, ValueError):
        return None
    return effective if effective > 0 else None


def _apply_statement_timeout(
    conn: "snowflake.connector.SnowflakeConnection", target_seconds: int
) -> None:
    """Lower the session's STATEMENT_TIMEOUT_IN_SECONDS to `target_seconds` unless an operator
    policy is already at or below it; never raise an existing stricter value.

    Fails closed: an effective value of 0 (unbounded), a missing row or an unparseable value
    all result in ALTER SESSION to the target. Any exception closes the connection before
    re-raising so a half-configured session is never handed back; the @sanitize_exceptions
    decorator on _connect() turns it into a SanitizedEngineError. `target_seconds` is always
    one of this module's own int constants, never caller input.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SHOW PARAMETERS LIKE '{_STATEMENT_TIMEOUT_PARAMETER}' IN SESSION",
                timeout=_SESSION_PARAMETER_PROBE_TIMEOUT_SECONDS,
            )
            effective = _parse_effective_statement_timeout(cur.fetchone())
            if effective is not None and effective <= target_seconds:
                return
            cur.execute(
                f"ALTER SESSION SET {_STATEMENT_TIMEOUT_PARAMETER} = {int(target_seconds)}",
                timeout=_SESSION_PARAMETER_PROBE_TIMEOUT_SECONDS,
            )
    except Exception:
        # Best-effort close, mirroring explain_estimate/execute_bounded's own discipline - a
        # failed close must never mask the real failure being re-raised.
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
        raise


@sanitize_exceptions("snowflake")
def _connect(
    statement_timeout_seconds: int = _NETWORK_TIMEOUT_SECONDS,
) -> "snowflake.connector.SnowflakeConnection":
    """Open a per-call connection whose session authority is exactly SNOWFLAKE_ROLE and whose
    statements carry a provider-side deadline of at most `statement_timeout_seconds`.

    `role=` only sets the session's PRIMARY role; Snowflake authorizes each statement against
    the primary role plus every ACTIVE secondary role, and the active set at login defaults to
    the user's DEFAULT_SECONDARY_ROLES (('ALL') for users created after behavior-change
    bundle 2024_07). `secondary_roles=` travels in the login request itself
    (SECONDARY_ROLES=NONE unless the operator opted into ALL), so the pin is in force before
    USE WAREHOUSE or any model-supplied text, with no extra round trip and no pre-pin
    window. tests/unit/test_snowflake_dependency_contract.py guards the kwarg's existence
    because the connector only WARNS on an unknown connect() kwarg (fail-open).

    `statement_timeout_seconds` defaults to the read-only paths' network timeout (60 s);
    execute_bounded passes its own 120 s watchdog bound. See _apply_statement_timeout for why
    it is applied read-then-lower rather than as a login-time session parameter.
    """
    config = load_snowflake_config()
    # Login-time session parameters (sent in the login request body, so in force before any
    # statement). MULTI_STATEMENT_COUNT=1 is the session-level half of the single-statement
    # pin described at _SINGLE_STATEMENT_COUNT; the per-call num_statements kwarg on the two
    # statements that carry model text is the other half.
    session_parameters = {"MULTI_STATEMENT_COUNT": _SINGLE_STATEMENT_COUNT}

    if config.private_key_path:
        conn = snowflake.connector.connect(
            account=config.account,
            user=config.user,
            role=config.role,
            authenticator="SNOWFLAKE_JWT",
            private_key_file=config.private_key_path,
            private_key_file_pwd=config.private_key_passphrase,
            login_timeout=_LOGIN_TIMEOUT_SECONDS,
            network_timeout=_NETWORK_TIMEOUT_SECONDS,
            secondary_roles=config.secondary_roles,
            session_parameters=session_parameters,
        )
    else:
        conn = snowflake.connector.connect(
            account=config.account,
            user=config.user,
            role=config.role,
            password=config.password,
            login_timeout=_LOGIN_TIMEOUT_SECONDS,
            network_timeout=_NETWORK_TIMEOUT_SECONDS,
            secondary_roles=config.secondary_roles,
            session_parameters=session_parameters,
        )

    _apply_statement_timeout(conn, statement_timeout_seconds)
    return conn


_WAREHOUSE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _validate_warehouse(warehouse: str) -> str:
    if not _WAREHOUSE_NAME_RE.fullmatch(warehouse):
        raise ValueError(
            f"Invalid warehouse name {warehouse!r}: must match [A-Za-z_][A-Za-z0-9_$]*"
        )
    return warehouse


# EXPLAIN itself doesn't estimate runtime — this is a deliberately conservative, documented
# placeholder assumption (30 seconds) used only to turn a byte/partition estimate into SOME
# dollar figure. This is the least-defensible part of the Snowflake estimate; tightening it
# (e.g. by correlating bytesAssigned with historical query_history runtimes for this
# warehouse) is the highest-value follow-up work once real usage data exists.
_ASSUMED_RUNTIME_HOURS = 30 / 3600

# Warehouse-history calibration (see docs/superpowers/specs/2026-09-15-warehouse-history-
# calibration-design.md): when the caller's own INFORMATION_SCHEMA.QUERY_HISTORY has a real
# (non-cache-hit) execution of this exact SQL text, use its average runtime instead of the
# coarse byte-size-tier heuristic above. _HISTORY_LOOKUP_TIMEOUT_SECONDS bounds the lookup
# per the design spec's Global Constraints — it's passed as cur.execute()'s own `timeout`
# kwarg below, which is the connector's client-side "timebomb" (cancels the query and raises
# after N seconds); this metadata read is a single synchronous call, not a real warehouse-
# billed job awaiting async completion, so it doesn't need execute_bounded's heavier
# poll/cancel loop — the connector's own per-call timeout is the right-sized tool here.
_HISTORY_LOOKUP_TIMEOUT_SECONDS = 5
_HISTORY_LOOKUP_RESULT_LIMIT = 500

# Warehouse-generation detection (see docs/superpowers/plans/2026-09-18-next-upgrade-plan.md
# Phase 4): explain_estimate() used to silently assume every warehouse was Gen1, but Gen2
# standard warehouses are now the default for new warehouses in most regions and bill at a
# different, cloud-provider-dependent rate (Snowflake's own Service Consumption Table,
# confirmed live 2026-09-18 — see snowflake_pricing.py's module docstring for the exact
# source/rates). This mirrors _lookup_historical_runtime's own try/except-degrade-to-None
# contract exactly, including its own short timeout bound.
_GENERATION_LOOKUP_TIMEOUT_SECONDS = 5
_CLOUD_PROVIDERS = ("AWS", "AZURE", "GCP")


def _normalize_sql_for_matching(sql_text: str) -> str:
    """Collapse whitespace runs to single spaces and strip - deliberately NOT case-folded,
    since quoted identifiers in Snowflake are case-sensitive and case-folding risks
    conflating genuinely different queries."""
    return " ".join(sql_text.split())


def _lookup_historical_runtime(
    conn: "snowflake.connector.SnowflakeConnection", sql_text: str
) -> HistoricalRuntimeSignal | None:
    """Best-effort - any failure (permission, network, malformed/None-valued row, timeout)
    returns None, never raises; no exception propagates out of explain_estimate because of
    this lookup. Cache-hit rows (bytes_scanned == 0) are excluded - averaging in a
    near-instant cached result would corrupt calibration toward underestimating future
    runtime (confirmed live 2026-09-15: a real 360ms/10,741,184-byte execution vs. an
    immediate 54ms/0-byte cache-hit repeat).

    INFORMATION_SCHEMA.QUERY_HISTORY is per-database, not global, and this connection never
    issues its own USE DATABASE (see _connect() / load_snowflake_config() - neither sets a
    default database). Rather than requiring a new, feature-specific database context, this
    fully-qualifies the table function against the built-in `SNOWFLAKE` system database,
    which every account has and which is visible to all users by default. Per Snowflake's own
    docs, INFORMATION_SCHEMA access is not among the object types that require an explicit
    ACCOUNTADMIN-only grant (unlike ACCOUNT_USAGE/READER_ACCOUNT_USAGE/ORGANIZATION_USAGE/
    DATA_SHARING_USAGE) - but that is an inference from documentation, not a live-tested
    result against this project's actual least-privilege role (DECISIONS.md #7 requires a
    role scoped to the objects being cost-estimated, never ACCOUNTADMIN). This assumption is
    exactly what the plan's Phase 4 live-verification task is meant to confirm or correct
    before it's treated as settled; until then, if a specific role somehow lacks access, the
    surrounding try/except degrades to None exactly like any other permission failure, so a
    wrong assumption here fails safe rather than breaking explain_estimate.
    """
    normalized_target = _normalize_sql_for_matching(sql_text)
    try:
        with conn.cursor() as cur:
            # RESULT_LIMIT is our own int constant, never caller input - same class of
            # interpolation as execute_bounded's LIMIT clause below.
            history_query = (
                "SELECT QUERY_TEXT, TOTAL_ELAPSED_TIME, BYTES_SCANNED "  # noqa: S608
                "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY("
                f"RESULT_LIMIT => {_HISTORY_LOOKUP_RESULT_LIMIT})) "
                "WHERE EXECUTION_STATUS = 'SUCCESS'"
            )
            cur.execute(history_query, timeout=_HISTORY_LOOKUP_TIMEOUT_SECONDS)
            rows = cur.fetchall()

        # Row-level type guards, not just a blanket except: a real QUERY_HISTORY row with a
        # None QUERY_TEXT or a non-numeric BYTES_SCANNED (both observed as plausible real
        # values) must be treated as a non-match, not crash the whole lookup - this whole
        # block sits inside the same try as the query above so any other malformed-response
        # shape still degrades to None rather than raising.
        matches_ms = [
            elapsed_ms
            for query_text, elapsed_ms, bytes_scanned in rows
            if isinstance(query_text, str)
            and isinstance(elapsed_ms, (int, float))
            and isinstance(bytes_scanned, (int, float))
            and bytes_scanned > 0
            and _normalize_sql_for_matching(query_text) == normalized_target
        ]
        if not matches_ms:
            return None
        avg_hours = (sum(matches_ms) / len(matches_ms)) / 1000 / 3600
        return HistoricalRuntimeSignal(avg_runtime_hours=avg_hours, sample_count=len(matches_ms))
    except Exception:  # noqa: BLE001 - best-effort, matches cancel()/close() discipline
        return None


@dataclass(frozen=True)
class _WarehouseFacts:
    """What SHOW WAREHOUSES and CURRENT_REGION() say about the warehouse a query is pinned to.

    `size_key` is the pricing-table key for the row's `size` text (`size_text`), or None when
    the text is not a size this project prices; `generation`/`cloud_provider` are None when
    their value is not one of the recognised ones. `state`/`auto_resume` are carried for the
    pending read-only warehouse-state guard (audit record read-only-tools-resume-suspended-
    warehouse) so it can reuse this single lookup instead of issuing another one.
    """

    name: str
    generation: str | None
    cloud_provider: str | None
    size_key: str | None
    size_text: str | None
    warehouse_type: str | None
    state: str | None
    auto_resume: str | None


@dataclass(frozen=True)
class _UnresolvedWarehouse:
    """SHOW WAREHOUSES answered, but its rows did not contain exactly one named `pinned_name`
    (the upper-cased, allowlisted name the query is pinned to) - so the warehouse the query
    will run on was not observed, and nothing else in the result may stand in for it.

    Distinct from the None a failed lookup returns on purpose: None takes DECISIONS.md #19's
    fail-open-with-caveat path (roles that cannot run SHOW WAREHOUSES keep working at the
    caller-asserted rate), whereas this makes _rate_for fail closed (DECISIONS.md #5). PR #86
    runtime verification (probe-3 p45) showed why the two must not share a value: a quoted
    lower-case "compute_wh" next to COMPUTE_WH made the old case-insensitive match see two
    rows, return None, and price a 6X-Large query at the caller-asserted X-Small rate.

    `candidates_presented` holds the first _MAX_UNRESOLVED_CANDIDATES_PRESENTED row names,
    each already passed through present_remote_text (they are warehouse-returned text);
    `candidate_count` is how many rows there were in total.
    """

    pinned_name: str
    candidates_presented: tuple[str, ...]
    candidate_count: int


# How many of the SHOW WAREHOUSES row names an _UnresolvedWarehouse carries into its caveat.
# LIKE's '_' wildcard can match many sibling warehouses and each presented name is itself
# bounded only at present_remote_text's 1,000 characters, so the count is capped here too.
_MAX_UNRESOLVED_CANDIDATES_PRESENTED = 5


_STANDARD_WAREHOUSE_TYPE = "STANDARD"

# SHOW WAREHOUSES reports `size` as display text ('X-Small', '4X-Large'); the pricing table is
# keyed the way Snowflake's own CREATE WAREHOUSE syntax spells sizes ('XSMALL', 'X4LARGE'). An
# explicit allowlist, not a regex: anything else (a new size, an Interactive/Snowpark tier
# label, garbage) is "not priced" and makes explain_estimate fail closed on a dollar cap.
_SHOW_SIZE_TO_PRICING_KEY: dict[str, str] = {
    "X-SMALL": "XSMALL",
    "XSMALL": "XSMALL",
    "SMALL": "SMALL",
    "MEDIUM": "MEDIUM",
    "LARGE": "LARGE",
    "X-LARGE": "XLARGE",
    "XLARGE": "XLARGE",
    "2X-LARGE": "X2LARGE",
    "XXLARGE": "X2LARGE",
    "X2LARGE": "X2LARGE",
    "3X-LARGE": "X3LARGE",
    "XXXLARGE": "X3LARGE",
    "X3LARGE": "X3LARGE",
    "4X-LARGE": "X4LARGE",
    "X4LARGE": "X4LARGE",
    "5X-LARGE": "X5LARGE",
    "X5LARGE": "X5LARGE",
    "6X-LARGE": "X6LARGE",
    "X6LARGE": "X6LARGE",
}


def _size_to_pricing_key(size_text: object) -> str | None:
    if not isinstance(size_text, str):
        return None
    return _SHOW_SIZE_TO_PRICING_KEY.get(size_text.strip().upper())


def _text_or_none(value: object, *, upper: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text.upper() if upper else text


def _cloud_provider_from_region(region_row: object) -> str | None:
    """CURRENT_REGION() returns "<CLOUD>_<REGION>" (e.g. "AWS_US_WEST_2") for most accounts, or
    "<region-group>.<CLOUD>_<REGION>" (e.g. "PUBLIC.AWS_US_WEST_2") for accounts whose
    organization spans multiple region groups - both shapes are documented examples in
    Snowflake's own CURRENT_REGION() reference."""
    if not isinstance(region_row, (tuple, list)) or not region_row:
        return None
    if not isinstance(region_row[0], str):
        return None
    region = region_row[0].rsplit(".", 1)[-1]
    cloud_provider = region.split("_", 1)[0].upper()
    return cloud_provider if cloud_provider in _CLOUD_PROVIDERS else None


def _lookup_warehouse_facts(
    conn: "snowflake.connector.SnowflakeConnection", warehouse: str | None
) -> _WarehouseFacts | _UnresolvedWarehouse | None:
    """Best-effort, non-privileged observation of the warehouse the query is pinned to - its
    size, type, generation and state from its SHOW WAREHOUSES row, plus the account's cloud
    provider from CURRENT_REGION() - used by explain_estimate() to bind the cap-gate rate to
    the warehouse that will actually execute instead of to the caller's warehouse_size string
    (DECISIONS.md #19). Mirrors _lookup_historical_runtime's contract for failures: a lookup
    that fails - permission, network, malformed response shape, timeout - or that finds no
    warehouse at all (an empty SHOW result, no CURRENT_WAREHOUSE()) returns None, never raises;
    explain_estimate() then degrades to the caller-asserted/Gen1 rate plus an explicit caveat.

    When `warehouse` is None the session's active warehouse is resolved with SELECT
    CURRENT_WAREHOUSE() first - it is what EXPLAIN just ran against and what execute_bounded
    will run against. The resolved name is interpolated into SHOW WAREHOUSES LIKE '<name>'
    exactly like a caller-supplied one, so it passes the same allowlist (DECISIONS.md #6).

    The row is read straight from the SHOW result via fetchall()/cursor.description - no
    RESULT_SCAN(LAST_QUERY_ID(-1)) SELECT (one statement fewer, same data). SHOW's LIKE is
    case-insensitive and treats '_' as a single-character wildcard, so the result can hold
    sibling warehouses: MYAWH for MY_WH, or a quoted lower-case "compute_wh" next to
    COMPUTE_WH. Exactly one row may bind - the one whose name EQUALS the upper-cased pinned
    name. USE WAREHOUSE on an unquoted identifier (the only kind _WAREHOUSE_NAME_RE admits)
    resolves to the upper-cased stored name, so that row is the warehouse the query runs on,
    and a differently-cased quoted sibling is a DIFFERENT warehouse, not another spelling of
    the same one. When SHOW answered with rows but no single row carries that name the result
    is an _UnresolvedWarehouse, never None, so _rate_for fails closed: PR #86 runtime
    verification (probe-3 p45) drove the old case-insensitive match with COMPUTE_WH (6X-Large)
    plus "compute_wh" (X-Small), got two matches -> None, and the 6X-Large query ran under a
    cap priced at X-Small. `generation` is a real SHOW WAREHOUSES column (behavior-change
    bundle 2025_07/bcr-2110, "currently either 1 or 2"); the `size`/`type` text shapes are
    pinned by tests/integration/test_snowflake_live.py.

    SHOW WAREHOUSES, CURRENT_WAREHOUSE() and CURRENT_REGION() are all ordinary, non-privileged
    SQL - no ACCOUNTADMIN or elevated grant is required (AGENTS.md / DECISIONS.md #7).
    """
    try:
        if warehouse is None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT CURRENT_WAREHOUSE()", timeout=_GENERATION_LOOKUP_TIMEOUT_SECONDS
                )
                current = cur.fetchone()
            if current is None or not isinstance(current[0], str):
                return None
            warehouse = current[0]
        validated_warehouse = _validate_warehouse(warehouse)
        with conn.cursor() as cur:
            cur.execute(
                f"SHOW WAREHOUSES LIKE '{validated_warehouse}'",
                timeout=_GENERATION_LOOKUP_TIMEOUT_SECONDS,
            )
            columns = [str(column[0]).lower() for column in (cur.description or ())]
            rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

            cur.execute("SELECT CURRENT_REGION()", timeout=_GENERATION_LOOKUP_TIMEOUT_SECONDS)
            region_row = cur.fetchone()
    except Exception:  # noqa: BLE001 - best-effort, matches _lookup_historical_runtime's own discipline
        return None

    if not rows:
        return None
    pinned_name = validated_warehouse.upper()
    matching = [row for row in rows if row.get("name") == pinned_name]
    if len(matching) != 1:
        return _UnresolvedWarehouse(
            pinned_name=pinned_name,
            candidates_presented=tuple(
                present_remote_text(str(row.get("name")))
                for row in rows[:_MAX_UNRESOLVED_CANDIDATES_PRESENTED]
            ),
            candidate_count=len(rows),
        )
    row = matching[0]
    generation = str(row.get("generation", "")).strip()
    return _WarehouseFacts(
        name=row["name"],
        generation=generation if generation in ("1", "2") else None,
        cloud_provider=_cloud_provider_from_region(region_row),
        size_key=_size_to_pricing_key(row.get("size")),
        size_text=_text_or_none(row.get("size")),
        warehouse_type=_text_or_none(row.get("type"), upper=True),
        state=_text_or_none(row.get("state"), upper=True),
        auto_resume=_text_or_none(row.get("auto_resume")),
    )


_CORTEX_CAVEAT = (
    "This estimate excludes Cortex AI Function ('AI Credits') cost — EXPLAIN's bytesAssigned "
    "only reflects warehouse compute/scan, not AI-inference calls inside the SQL."
)
_FAIL_CLOSED_HINT = (
    "no dollar estimate is produced, so run_query_bounded refuses a max_estimated_cost_usd cap "
    "for it; omit the dollar cap to run on this warehouse."
)


def _is_valid_byte_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _rate_for(
    facts: _WarehouseFacts | _UnresolvedWarehouse | None, asserted_size_key: str
) -> tuple[float | None, str, list[str]]:
    """Pick the credit rate the cap gate prices at: (rate, effective size key, caveats).

    The rate is never below the observed warehouse - the caller's warehouse_size may raise the
    size, never lower it. `rate` is None when the observed warehouse is of a type or size the
    rate table does not model, and likewise when SHOW WAREHOUSES answered but no single row
    was the pinned warehouse (_UnresolvedWarehouse) - there is nothing to price at, so the cap
    gate must not be handed a figure; explain_estimate then returns estimated_cost_usd=None so
    run_query_bounded refuses a dollar cap (DECISIONS.md #5: an unenforceable cap must never
    look enforced). A failed lookup (facts is None) keeps the caller-asserted/default size at
    the Gen1 rate with an explicit caveat, so roles that cannot run SHOW WAREHOUSES keep
    working (DECISIONS.md #19).
    """
    if facts is None:
        caveat = (
            "Warehouse generation and size could not be determined (no warehouse specified or "
            "resolvable, or the SHOW WAREHOUSES/cloud-provider lookup failed) — assuming Gen1 "
            f"credit rates at the caller-asserted {asserted_size_key} size; actual cost will be "
            "higher than this estimate if the warehouse is really Gen2 or larger."
        )
        return credits_per_hour(asserted_size_key), asserted_size_key, [caveat]
    if isinstance(facts, _UnresolvedWarehouse):
        # pinned_name passed _WAREHOUSE_NAME_RE and is upper-cased, so it is safe to echo raw;
        # the candidate names are warehouse-returned text and arrive already presented.
        shown = ", ".join(facts.candidates_presented)
        hidden = facts.candidate_count - len(facts.candidates_presented)
        if hidden > 0:
            shown = f"{shown} and {hidden} more"
        caveat = (
            f"Warehouse {facts.pinned_name} could not be resolved to exactly one SHOW WAREHOUSES "
            f"row: the {facts.candidate_count} row(s) returned ({shown}) do not contain exactly "
            f"one named {facts.pinned_name} (unquoted identifiers are stored upper-cased, so a "
            "differently-cased quoted warehouse is a different warehouse), and the pinned "
            f"warehouse's size and type were not observed — {_FAIL_CLOSED_HINT}"
        )
        return None, asserted_size_key, [caveat]
    if facts.warehouse_type != _STANDARD_WAREHOUSE_TYPE:
        caveat = (
            f"Warehouse {facts.name} is of type {facts.warehouse_type!r}, which this project's "
            f"rate table does not model (only STANDARD warehouses are priced) — {_FAIL_CLOSED_HINT}"
        )
        return None, asserted_size_key, [caveat]
    if facts.size_key is None:
        caveat = (
            f"Warehouse {facts.name} reports size {repr(facts.size_text)[:32]}, which this "
            f"project's rate table does not recognise — {_FAIL_CLOSED_HINT}"
        )
        return None, asserted_size_key, [caveat]

    caveats: list[str] = []
    size_key = max(asserted_size_key, facts.size_key, key=credits_per_hour)
    if size_key != asserted_size_key:
        caveats.append(
            f"warehouse_size {asserted_size_key!r} understated the pinned warehouse {facts.name} "
            f"({facts.size_text}, {size_key}) — priced at the observed size; the warehouse_size "
            "argument can only raise the estimate, never lower it."
        )
    if facts.generation is not None and facts.cloud_provider is not None:
        rate = credits_per_hour(
            size_key, generation=facts.generation, cloud_provider=facts.cloud_provider
        )
        if facts.generation == "2":
            caveats.append(
                f"Warehouse generation detected as Gen2 on {facts.cloud_provider} — billed at "
                "Gen2's higher per-hour credit rate (per Snowflake's Service Consumption "
                "Table), not Gen1's."
            )
        else:
            caveats.append(
                f"Warehouse generation detected as Gen1 on {facts.cloud_provider} — billed at "
                "Gen1's per-hour credit rate."
            )
        return rate, size_key, caveats
    caveats.append(
        f"Warehouse generation could not be determined for {facts.name} (SHOW WAREHOUSES "
        "reported an unrecognised generation, or CURRENT_REGION() an unrecognised cloud "
        "provider) — assuming Gen1 credit rates; actual cost will be higher than this estimate "
        "if the warehouse is really Gen2."
    )
    return credits_per_hour(size_key), size_key, caveats


def _price_for(configured_edition: str, asserted_edition: str) -> tuple[float, list[str]]:
    """USD/credit at the higher of the operator-configured edition (SNOWFLAKE_EDITION) and the
    caller-asserted one: the tool argument may only raise the price, never lower it. Edition
    is an account property the server cannot observe without ORGADMIN, hence configuration."""
    effective = max(configured_edition.lower(), asserted_edition.lower(), key=usd_per_credit)
    caveats: list[str] = []
    if effective != asserted_edition.lower():
        caveats.append(
            f"edition {asserted_edition!r} is below the operator-configured "
            f"SNOWFLAKE_EDITION={configured_edition!r} — priced at the configured edition; the "
            "edition argument can only raise the estimate, never lower it."
        )
    return usd_per_credit(effective), caveats


def _runtime_for(
    conn: "snowflake.connector.SnowflakeConnection",
    sql: str,
    bytes_assigned: object,
    size_key: str,
) -> tuple[float | None, list[str]]:
    """Runtime hours for the dollar figure: a historical run of this exact SQL when there is
    one, else the byte-tier heuristic - but only from a real, non-negative integer bytesAssigned.
    A null/non-integer value used to flow into scale_runtime_hours as None and yield the
    information-free baseline figure DECISIONS.md #5 forbids; it now yields None (no dollar
    estimate) unless the query-dependent historical signal exists."""
    historical = _lookup_historical_runtime(conn, sql)
    if historical is not None:
        caveat = (
            f"Cost is informed by {historical.sample_count} historical run(s) of this "
            f"exact query, averaging {historical.avg_runtime_hours * 3600:.1f}s — more "
            "reliable than the coarse size-tier heuristic below."
        )
        return historical.avg_runtime_hours, [caveat]
    if _is_valid_byte_count(bytes_assigned):
        caveat = (
            f"Cost assumes a baseline {int(_ASSUMED_RUNTIME_HOURS * 3600)}-second runtime "
            f"on a {size_key} warehouse, scaled up by a coarse size tier based on "
            f"{bytes_assigned} bytes scanned — still a heuristic, not derived from this "
            "query's actual expected runtime."
        )
        return scale_runtime_hours(bytes_assigned, baseline_hours=_ASSUMED_RUNTIME_HOURS), [caveat]
    caveat = (
        f"EXPLAIN returned no usable bytesAssigned figure ({repr(bytes_assigned)[:32]}) — the "
        "byte estimate is unavailable, not zero, and without a historical runtime signal no "
        "dollar estimate is produced; run_query_bounded refuses a byte or dollar cap for this "
        "query."
    )
    return None, [caveat]


@sanitize_exceptions("snowflake")
def explain_estimate(
    sql: str,
    warehouse: str | None,
    warehouse_size: str = "XSMALL",
    edition: str = "standard",
) -> CostEstimate:
    """Estimate Snowflake query cost via EXPLAIN. Always UPPER_BOUND — never PRECISE — since
    Snowflake bills by warehouse-time, not bytes, and EXPLAIN's byte/partition figures are
    themselves documented upper bounds ("runtime optimizations... can reduce the number of
    partitions and bytes scanned").

    The dollar figure is priced at the warehouse the query is pinned to as observed through
    SHOW WAREHOUSES (size/type/generation) - never below it: `warehouse_size` and `edition`
    can only raise the estimate. Warehouse types the rate table does not model, and a plan
    without a usable bytesAssigned, yield estimated_cost_usd=None so run_query_bounded fails
    closed on a dollar cap (DECISIONS.md #5, #19).
    """
    # Validate every caller-asserted input before connecting: an unknown warehouse name, size
    # or edition is a clear (sanitized) ValueError that never costs a connection or a lookup.
    validated_warehouse = _validate_warehouse(warehouse) if warehouse is not None else None
    asserted_size_key = warehouse_size.upper()
    credits_per_hour(asserted_size_key)
    usd_per_credit(edition)
    config = load_snowflake_config()

    conn = _connect()
    try:
        with conn.cursor() as cur:
            if validated_warehouse is not None:
                cur.execute(f"USE WAREHOUSE {validated_warehouse}")
            # num_statements=1: Snowflake rejects the whole text if it is not exactly one
            # statement, so a ';'-separated trailing statement can neither execute inside this
            # read-only-advertised estimate nor be silently discarded (see
            # _SINGLE_STATEMENT_COUNT).
            cur.execute(f"EXPLAIN USING JSON {sql}", num_statements=_SINGLE_STATEMENT_COUNT)
            row = cur.fetchone()

        if row is None:
            raise ValueError("EXPLAIN USING JSON returned no rows")

        plan = json.loads(row[0])
        bytes_assigned = plan["GlobalStats"]["bytesAssigned"]

        facts = _lookup_warehouse_facts(conn, warehouse)
        rate, size_key, rate_caveats = _rate_for(facts, asserted_size_key)
        price, price_caveats = _price_for(config.edition, edition)
        runtime_hours, runtime_caveats = _runtime_for(conn, sql, bytes_assigned, size_key)

        estimated_cost_usd = (
            None
            if rate is None or runtime_hours is None
            else round(rate * price * runtime_hours, 6)
        )
        return CostEstimate(
            engine="snowflake",
            accuracy_tier=AccuracyTier.UPPER_BOUND,
            estimated_bytes=bytes_assigned if _is_valid_byte_count(bytes_assigned) else None,
            estimated_cost_usd=estimated_cost_usd,
            caveats=[_CORTEX_CAVEAT, *rate_caveats, *price_caveats, *runtime_caveats],
        )
    finally:
        # Best-effort close, mirroring the cancel-is-best-effort discipline in execute_bounded
        # below — a failed close must never mask the real return value/exception above.
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


# dust-tt/dust's precedent for bounding a long-running warehouse job: poll every 2s, give
# up (and cancel) after 2 minutes total. Snowflake's own get_results_from_sfqid() has an
# internal retry loop, but no overall wall-clock cap — a query stuck behind slot contention
# or a suspended/cold warehouse would otherwise block this tool call indefinitely, still
# burning warehouse-seconds the whole time, which defeats the point of a "bounded" tool.
_POLL_INTERVAL_SECONDS = 2
_MAX_EXECUTION_WAIT_SECONDS = 120


def _cancel_query(conn: "snowflake.connector.SnowflakeConnection", query_id: str) -> None:
    """Best-effort cancel; a failed cancel must not mask the TimeoutError the caller raises.

    `query_id` is connector-generated (cur.sfqid), not caller input — but it still gets
    validated as a UUID before reaching raw SQL text, same defense-in-depth discipline as
    _validate_warehouse and _validate_project_id use for genuinely caller-supplied values.
    The CANONICAL text (str(uuid.UUID(...))) is what gets interpolated, never the raw string:
    uuid.UUID() also accepts braces, urn: prefixes and upper-case hex, so validating and then
    interpolating the original would still let non-canonical text into the SQL literal.
    """
    try:
        canonical_query_id = str(uuid.UUID(query_id))
        with conn.cursor() as cur:
            cur.execute(f"SELECT SYSTEM$CANCEL_QUERY('{canonical_query_id}')")
    except Exception:  # noqa: BLE001, S110
        pass


@sanitize_exceptions("snowflake")
def execute_bounded(
    sql: str, warehouse: str | None, max_rows: int | None
) -> tuple[list[dict], int, bool]:
    # The session deadline matches this function's own watchdog bound, so the warehouse-side
    # query dies on its own even if this process is gone before _cancel_query can run.
    conn = _connect(statement_timeout_seconds=_MAX_EXECUTION_WAIT_SECONDS)
    try:
        wrapped_sql = sql
        if max_rows is not None:
            # `sql` is the caller's own query, passed as this tool's actual `sql` parameter -
            # wrapping their query in a LIMIT subquery to cap rows is this function's job, not
            # untrusted input reaching a query built from a different source. The trailing
            # `;` and any trailing comments go first (sql_scope.single_statement_body, shared
            # with the other engines): either makes `(...) AS x LIMIT n` a syntax error.
            inner_sql = single_statement_body(sql, "snowflake")
            wrapped_sql = f"SELECT * FROM ({inner_sql}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

        with conn.cursor(snowflake.connector.DictCursor) as cur:
            if warehouse is not None:
                cur.execute(f"USE WAREHOUSE {_validate_warehouse(warehouse)}")
            # execute_async forwards **kwargs to execute, so num_statements=1 reaches the
            # request as statement_params {'MULTI_STATEMENT_COUNT': 1} - the billable path
            # gets the same server-side single-statement pin as EXPLAIN above.
            cur.execute_async(wrapped_sql, num_statements=_SINGLE_STATEMENT_COUNT)
            query_id = cur.sfqid
            if query_id is None:
                raise SanitizedEngineError("execute_async did not return a query id")

            elapsed_seconds = 0
            status = conn.get_query_status(query_id)
            while conn.is_still_running(status):
                if elapsed_seconds >= _MAX_EXECUTION_WAIT_SECONDS:
                    _cancel_query(conn, query_id)
                    logger.warning(
                        "engine=snowflake execute_bounded timed out after %ss, "
                        "cancelled query_id=%s",
                        _MAX_EXECUTION_WAIT_SECONDS,
                        query_id,
                    )
                    raise TimeoutError(
                        f"Query exceeded {_MAX_EXECUTION_WAIT_SECONDS}s and was cancelled "
                        f"(query_id={query_id})."
                    )
                time.sleep(_POLL_INTERVAL_SECONDS)
                elapsed_seconds += _POLL_INTERVAL_SECONDS
                status = conn.get_query_status(query_id)

            conn.get_query_status_throw_if_error(query_id)
            cur.get_results_from_sfqid(query_id)
            rows = cur.fetchall()

        row_cap_hit = max_rows is not None and len(rows) > max_rows
        if row_cap_hit:
            rows = rows[:max_rows]

        return rows, len(rows), row_cap_hit
    finally:
        # Outermost finally: runs on every exit path (success, timeout-raised, real-query-
        # error-reraised) after all existing cursor/cancel logic above. Best-effort, mirroring
        # _cancel_query's own discipline — a failed close must never mask the real outcome.
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


def check_credentials(warehouse: str | None = None) -> CredentialCheckResult:
    """Verify Snowflake credentials/connectivity without running EXPLAIN or any real query.

    Deliberately does NOT use @sanitize_exceptions: a failed check is the expected, useful
    result here, not an error condition — this function always returns a
    CredentialCheckResult (never raises for a credential/connectivity failure) so a caller
    can distinguish "not configured yet" from "actually broken" before attempting a real
    estimate_query_cost/run_query_bounded call. The failure detail goes through the same
    redact_secrets -> present_remote_text chain as every other outbound error text (bounded,
    repr()-quoted) and is recorded as one WARNING - also when _connect() already sanitized
    its own failure, where re-presenting the already-safe text just adds a layer of quotes.
    """
    conn: snowflake.connector.SnowflakeConnection | None = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            if warehouse is not None:
                cur.execute(f"USE WAREHOUSE {_validate_warehouse(warehouse)}")
            # CURRENT_SECONDARY_ROLES() is reported so the operator can SEE the effective
            # authority of this session (primary role plus active secondary roles), not just
            # the primary role the config asked for - see _connect().
            cur.execute(
                "SELECT CURRENT_ROLE(), CURRENT_SECONDARY_ROLES(), CURRENT_WAREHOUSE(), "
                "CURRENT_ACCOUNT()"
            )
            row = cur.fetchone()
            if row is None:
                raise SanitizedEngineError("session context query returned no rows")
            role, secondary_roles, current_warehouse, account = row
    except Exception as exc:  # noqa: BLE001 - reporting failure as data, not raising, by design
        raw_detail = str(exc)
        safe_detail = present_remote_text(redact_secrets(raw_detail), total_chars=len(raw_detail))
        logger.warning("engine=snowflake check_credentials_failed detail=%s", safe_detail)
        return CredentialCheckResult(engine="snowflake", ok=False, detail=safe_detail)
    finally:
        # conn is None if _connect() itself raised — nothing to close in that case. A failed
        # close() is best-effort and must never mask the success return below.
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110
                pass

    return CredentialCheckResult(
        engine="snowflake",
        ok=True,
        detail=(
            f"Authenticated as role '{role}' (secondary roles: {secondary_roles}) on "
            f"account '{account}', warehouse '{current_warehouse}'."
        ),
    )
