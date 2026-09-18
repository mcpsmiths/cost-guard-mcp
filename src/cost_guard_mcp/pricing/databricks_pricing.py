"""Databricks Serverless SQL warehouse pricing.

Source: https://www.databricks.com/product/pricing/databricks-lakehouse (DBU-per-hour
table cross-checked against https://azure.microsoft.com/en-us/pricing/details/databricks,
both confirmed identical across Classic/Pro/Serverless — only the $/DBU dollar rate
differs by tier). Live-verified 2026-09-14. Re-fetch and update before each release;
Databricks has changed these before.

5X-Large: NOT on the primary GA pricing table above (it's still Public Preview per
https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior — "The 5X-Large
cluster size is in Public Preview for pro and serverless SQL warehouses in all regions").
Rate sourced instead from a Databricks-employee-authored technical blog post announcing the
feature: "Introducing 5XL SQL Warehouses: A Practical Guide to Meeting SLAs"
(community.databricks.com/t5/technical-blog/.../ba-p/156332, published 2026-05-11), whose
worked TCO example states "Hourly rate | 528 DBU/hr | 1,042 DBU/hr" for 4XL vs 5XL — the
528 figure matches this table's confirmed 4X-Large value exactly, which is why the 1,042
figure for 5XL is trusted here despite not (yet) appearing on the GA price list. Re-verify
against the primary pricing page once 5X-Large exits Public Preview.

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
    "5X-LARGE": 1042,
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
