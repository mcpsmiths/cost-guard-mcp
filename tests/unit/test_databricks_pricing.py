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
