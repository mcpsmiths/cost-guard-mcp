from cost_guard_mcp.pricing.bigquery_pricing import ON_DEMAND_USD_PER_TIB, TIB_IN_BYTES


def test_on_demand_rate_matches_confirmed_current_pricing():
    # $6.25/TiB, US multi-region on-demand, confirmed live 2026-09-12 (cloud.google.com/bigquery/pricing)
    assert ON_DEMAND_USD_PER_TIB == 6.25


def test_tib_in_bytes_is_correct():
    assert TIB_IN_BYTES == 1024**4
