"""Snowflake Gen1/Gen2 standard-warehouse credit rates and On-Demand Platform Credit pricing.

Source: Snowflake's own Service Consumption Table
(https://www.snowflake.com/legal-files/CreditConsumptionTable.pdf). The Gen1 table and the
On-Demand USD/credit table were first confirmed live 2026-09-09/2026-09-12; both were
re-fetched and re-confirmed unchanged on 2026-09-18 (document now stamped "Effective:
September 16, 2026") as part of adding the Gen2 table below in the same pass. This is a
versioned, dated document — re-fetch and update these constants periodically rather than
treating them as permanent.

Gen2 rates (Table 1(b) of the document above, confirmed live 2026-09-18): Gen2 standard
warehouses bill at a higher, cloud-provider-dependent rate than Gen1 of the same size — 1.35x
Gen1 on AWS and GCP, 1.25x on Azure. Snowflake's own "Snowflake generation 2 standard
warehouses" doc (docs.snowflake.com/en/user-guide/warehouses-gen2) confirms Gen2 is now the
default generation for new standard warehouses in every region that supports it (behavior-
change bundle 2026_03) — this is the common case going forward, not an edge case. Gen2 is
NOT available at the X5LARGE/X6LARGE sizes (Table 1(b) stops at X4LARGE — those two sizes
exist only in the Gen1 table); `credits_per_hour()` falls back to the Gen1 rate for those two
sizes even when generation="2" is explicitly requested, matching what Snowflake itself does
(a Gen2 X5LARGE/X6LARGE cannot exist).

LIMITATION (documented, updated from v1's Gen1-only scope): this table covers Gen1 and Gen2
standard warehouses at AWS/Azure/GCP, and the default (Standard/Enterprise/Business Critical)
editions at AWS US East / Azure East US / GCP us-central1 On-Demand rates. Snowpark-optimized
warehouses, Interactive warehouses, and non-US-East regions have different rates in the same
source document — extend this table if your deployment needs them.
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

# Gen2 credit rates are cloud-provider-dependent (Table 1(b)), unlike Gen1's single flat
# table shared by every cloud. Confirmed live 2026-09-18 directly from Snowflake's Service
# Consumption Table PDF (effective 2026-09-16) — AWS/GCP both 1.35x the Gen1 rate at every
# size, Azure 1.25x. No X5LARGE/X6LARGE entries: Gen2 does not support those two sizes.
_GEN2_CREDITS_PER_HOUR: dict[tuple[str, str], float] = {
    ("AWS", "XSMALL"): 1.35,
    ("AWS", "SMALL"): 2.7,
    ("AWS", "MEDIUM"): 5.4,
    ("AWS", "LARGE"): 10.8,
    ("AWS", "XLARGE"): 21.6,
    ("AWS", "X2LARGE"): 43.2,
    ("AWS", "X3LARGE"): 86.4,
    ("AWS", "X4LARGE"): 172.8,
    ("AZURE", "XSMALL"): 1.25,
    ("AZURE", "SMALL"): 2.5,
    ("AZURE", "MEDIUM"): 5.0,
    ("AZURE", "LARGE"): 10.0,
    ("AZURE", "XLARGE"): 20.0,
    ("AZURE", "X2LARGE"): 40.0,
    ("AZURE", "X3LARGE"): 80.0,
    ("AZURE", "X4LARGE"): 160.0,
    ("GCP", "XSMALL"): 1.35,
    ("GCP", "SMALL"): 2.7,
    ("GCP", "MEDIUM"): 5.4,
    ("GCP", "LARGE"): 10.8,
    ("GCP", "XLARGE"): 21.6,
    ("GCP", "X2LARGE"): 43.2,
    ("GCP", "X3LARGE"): 86.4,
    ("GCP", "X4LARGE"): 172.8,
}

# AWS US East (N. Virginia) / Azure East US / GCP us-central1 — On-Demand Platform Credit
# pricing, Table 2(a) of the Service Consumption Table above.
_USD_PER_CREDIT_BY_EDITION: dict[str, float] = {
    "standard": 2.00,
    "enterprise": 3.00,
    "business_critical": 4.00,
    "vps": 6.00,
}


def credits_per_hour(
    warehouse_size: str, generation: str = "1", cloud_provider: str = "AWS"
) -> float:
    """Credits/hour for a standard warehouse of the given size, generation, and cloud
    provider. Defaults to today's Gen1-assumed behavior (generation="1") when the caller
    doesn't know or doesn't pass generation/cloud_provider, so every pre-existing call site
    stays backward-compatible. A generation="2" request falls back to the Gen1 rate whenever
    the (cloud_provider, size) pair has no Gen2 entry — an unrecognized cloud_provider string,
    or X5LARGE/X6LARGE (which Gen2 doesn't support at all) — rather than raising, since Gen1
    is the documented, safe, always-available rate for any standard warehouse size.
    """
    size = warehouse_size.upper()
    if size not in _GEN1_CREDITS_PER_HOUR:
        raise ValueError(
            f"unknown Snowflake warehouse size '{warehouse_size}'. "
            f"Known sizes: {sorted(_GEN1_CREDITS_PER_HOUR)}."
        )
    if generation == "2":
        gen2_rate = _GEN2_CREDITS_PER_HOUR.get((cloud_provider.upper(), size))
        if gen2_rate is not None:
            return gen2_rate
    return _GEN1_CREDITS_PER_HOUR[size]


def usd_per_credit(edition: str = "standard") -> float:
    key = edition.lower()
    if key not in _USD_PER_CREDIT_BY_EDITION:
        raise ValueError(
            f"unknown Snowflake edition '{edition}'. Known editions: {sorted(_USD_PER_CREDIT_BY_EDITION)}."
        )
    return _USD_PER_CREDIT_BY_EDITION[key]
