import pytest

from cost_guard_mcp.pricing.snowflake_pricing import credits_per_hour, usd_per_credit


def test_credits_per_hour_doubles_at_each_gen1_size_step():
    assert credits_per_hour("XSMALL") == 1.0
    assert credits_per_hour("SMALL") == 2.0
    assert credits_per_hour("MEDIUM") == 4.0
    assert credits_per_hour("LARGE") == 8.0
    assert credits_per_hour("XLARGE") == 16.0
    assert credits_per_hour("X6LARGE") == 512.0


def test_credits_per_hour_rejects_unknown_size():
    with pytest.raises(ValueError, match="unknown Snowflake warehouse size"):
        credits_per_hour("MEGA_LARGE")


def test_usd_per_credit_standard_edition_default():
    # $2.00/credit, AWS US East / Azure East US / GCP us-central1, Standard edition —
    # confirmed live in the Snowflake Service Consumption Table, effective 2026-09-09.
    assert usd_per_credit("standard") == 2.00


def test_usd_per_credit_scales_by_edition():
    assert usd_per_credit("enterprise") == 3.00  # 1.5x standard
    assert usd_per_credit("business_critical") == 4.00  # 2x standard
