"""Snowflake Gen1 standard-warehouse credit rates and On-Demand Platform Credit pricing.

Source: Snowflake's own Service Consumption Table
(https://www.snowflake.com/legal-files/CreditConsumptionTable.pdf), effective 2026-09-09,
confirmed live during the 2026-09-12 research refresh. This is a versioned, dated document —
re-fetch and update these constants periodically rather than treating them as permanent.

LIMITATION (documented, not fixed in v1): this table only covers Gen1 standard warehouses
and the default (Standard/Enterprise/Business Critical) editions at AWS US East / Azure East
US / GCP us-central1 rates. Gen2, Snowpark-optimized, Interactive warehouses, and non-US-East
regions have different rates in the same source document — extend this table if your
deployment needs them.
"""

_GEN1_CREDITS_PER_HOUR: dict[str, float] = {
    "XSMALL": 1.0,
    "SMALL": 2.0,
    "MEDIUM": 4.0,
    "LARGE": 8.0,
    "XLARGE": 16.0,
    "X2LARGE": 32.0,
    "X3LARGE": 64.0,
    "X4LARGE": 128.0,
    "X5LARGE": 256.0,
    "X6LARGE": 512.0,
}

# AWS US East (N. Virginia) / Azure East US / GCP us-central1 — On-Demand Platform Credit
# pricing, Table 2(a) of the Service Consumption Table above.
_USD_PER_CREDIT_BY_EDITION: dict[str, float] = {
    "standard": 2.00,
    "enterprise": 3.00,
    "business_critical": 4.00,
    "vps": 6.00,
}


def credits_per_hour(warehouse_size: str) -> float:
    size = warehouse_size.upper()
    if size not in _GEN1_CREDITS_PER_HOUR:
        raise ValueError(
            f"unknown Snowflake warehouse size '{warehouse_size}'. "
            f"Known sizes: {sorted(_GEN1_CREDITS_PER_HOUR)}."
        )
    return _GEN1_CREDITS_PER_HOUR[size]


def usd_per_credit(edition: str = "standard") -> float:
    key = edition.lower()
    if key not in _USD_PER_CREDIT_BY_EDITION:
        raise ValueError(
            f"unknown Snowflake edition '{edition}'. Known editions: {sorted(_USD_PER_CREDIT_BY_EDITION)}."
        )
    return _USD_PER_CREDIT_BY_EDITION[key]
