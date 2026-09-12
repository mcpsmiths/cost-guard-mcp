import json
import re

import snowflake.connector

from cost_guard_mcp.config import load_snowflake_config
from cost_guard_mcp.errors import sanitize_exceptions
from cost_guard_mcp.pricing.snowflake_pricing import credits_per_hour, usd_per_credit
from cost_guard_mcp.types import AccuracyTier, CostEstimate


@sanitize_exceptions("snowflake")
def _connect() -> "snowflake.connector.SnowflakeConnection":
    config = load_snowflake_config()

    if config.private_key_path:
        return snowflake.connector.connect(
            account=config.account,
            user=config.user,
            role=config.role,
            authenticator="SNOWFLAKE_JWT",
            private_key_file=config.private_key_path,
            private_key_file_pwd=config.private_key_passphrase,
        )

    return snowflake.connector.connect(
        account=config.account,
        user=config.user,
        role=config.role,
        password=config.password,
    )


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
    partitions and bytes scanned")."""
    conn = _connect()
    with conn.cursor() as cur:
        if warehouse:
            cur.execute(f"USE WAREHOUSE {_validate_warehouse(warehouse)}")
        cur.execute(f"EXPLAIN USING JSON {sql}")
        row = cur.fetchone()

    plan = json.loads(row[0])
    global_stats = plan["GlobalStats"]
    bytes_assigned = global_stats["bytesAssigned"]

    rate = credits_per_hour(warehouse_size)
    price = usd_per_credit(edition)
    estimated_cost_usd = rate * price * _ASSUMED_RUNTIME_HOURS

    return CostEstimate(
        engine="snowflake",
        accuracy_tier=AccuracyTier.UPPER_BOUND,
        estimated_bytes=bytes_assigned,
        estimated_cost_usd=round(estimated_cost_usd, 6),
        caveats=[
            "This estimate excludes Cortex AI Function ('AI Credits') cost — EXPLAIN's "
            "bytesAssigned only reflects warehouse compute/scan, not AI-inference calls "
            "inside the SQL.",
            f"Cost assumes a {int(_ASSUMED_RUNTIME_HOURS * 3600)}-second runtime on a "
            f"{warehouse_size} warehouse — a rough placeholder, not derived from this "
            "query's actual expected runtime.",
        ],
    )
