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


def test_usd_per_credit_rejects_unknown_edition():
    with pytest.raises(ValueError, match="unknown Snowflake edition"):
        usd_per_credit("not_a_real_edition")


def test_usd_per_credit_vps_edition():
    assert usd_per_credit("vps") == 6.00


def test_usd_per_credit_is_case_insensitive():
    assert usd_per_credit("STANDARD") == usd_per_credit("standard")


def test_credits_per_hour_is_case_insensitive():
    assert credits_per_hour("xsmall") == credits_per_hour("XSMALL")


def test_credits_per_hour_defaults_to_gen1_when_generation_and_provider_omitted():
    # Backward-compatibility guarantee: every pre-existing call site that only ever passed
    # warehouse_size must keep getting exactly today's Gen1 rate.
    assert credits_per_hour("MEDIUM") == 4.0


def test_credits_per_hour_gen1_explicit_matches_default():
    assert credits_per_hour("MEDIUM", generation="1") == credits_per_hour("MEDIUM")


def test_credits_per_hour_gen2_aws_rate():
    # 1.35x Gen1 on AWS — confirmed live 2026-09-18 against Snowflake's Service Consumption
    # Table (Table 1(b)), effective 2026-09-16.
    assert credits_per_hour("XSMALL", generation="2", cloud_provider="AWS") == 1.35
    assert credits_per_hour("MEDIUM", generation="2", cloud_provider="AWS") == 5.4
    assert credits_per_hour("X4LARGE", generation="2", cloud_provider="AWS") == 172.8


def test_credits_per_hour_gen2_gcp_rate_matches_aws():
    assert credits_per_hour("LARGE", generation="2", cloud_provider="GCP") == credits_per_hour(
        "LARGE", generation="2", cloud_provider="AWS"
    )


def test_credits_per_hour_gen2_azure_rate_is_lower_than_aws():
    # 1.25x Gen1 on Azure vs. 1.35x on AWS/GCP — Azure is deliberately cheaper.
    assert credits_per_hour("LARGE", generation="2", cloud_provider="AZURE") == 10.0
    assert credits_per_hour("LARGE", generation="2", cloud_provider="AZURE") < credits_per_hour(
        "LARGE", generation="2", cloud_provider="AWS"
    )


def test_credits_per_hour_gen2_cloud_provider_is_case_insensitive():
    assert credits_per_hour("SMALL", generation="2", cloud_provider="aws") == credits_per_hour(
        "SMALL", generation="2", cloud_provider="AWS"
    )


def test_credits_per_hour_gen2_falls_back_to_gen1_for_unsupported_size():
    # Gen2 does not exist at X5LARGE/X6LARGE (Table 1(b) stops at X4LARGE) — a generation="2"
    # request for either size must safely fall back to the Gen1 rate, not raise.
    assert credits_per_hour("X5LARGE", generation="2", cloud_provider="AWS") == 256.0
    assert credits_per_hour("X6LARGE", generation="2", cloud_provider="AWS") == 512.0


def test_credits_per_hour_gen2_falls_back_to_gen1_for_unknown_cloud_provider():
    assert credits_per_hour("MEDIUM", generation="2", cloud_provider="ORACLE_CLOUD") == 4.0


def test_credits_per_hour_unknown_generation_string_falls_back_to_gen1():
    # Only the literal "2" opts into the Gen2 table — any other/unexpected generation string
    # (e.g. a future "3", or a malformed lookup result) must degrade safely to Gen1, not raise.
    assert credits_per_hour("MEDIUM", generation="unknown") == 4.0
