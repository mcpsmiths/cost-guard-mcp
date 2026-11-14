"""Pin the documented runtime-scaling claims to the code, from both sides.

The dollar figure on Snowflake (no exact-text history hit) and on Databricks is
`rate x price x scale_runtime_hours(bytes, baseline_hours=30 s)`. The module docstring once
said that fallback "biases toward OVER-estimating ... never under"; it is a floored, monotone,
capped four-step function of bytes alone, so it can under-estimate (a compute-heavy query on a
small scan is priced at the 30 s floor; no scan is priced above 3000 s). The docstring and the
README's Known limitations now say what the code does. These tests make either side fail when
it changes alone: change a tier, the baseline or the cap and the docs assertions break; edit
the docs numbers and the code assertions break.
"""

import inspect
import re
from pathlib import Path

import pytest

from cost_guard_mcp.engines import bigquery, databricks, snowflake
from cost_guard_mcp.pricing import runtime_scaling
from cost_guard_mcp.pricing.bigquery_pricing import ON_DEMAND_USD_PER_TIB
from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU
from cost_guard_mcp.pricing.runtime_scaling import scale_runtime_hours

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
INTEGRATION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "integration.yml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

_GIB = 1024**3
_TIB = 1024**4

_BASELINE_HOURS = snowflake._ASSUMED_RUNTIME_HOURS
_BASELINE_SECONDS = round(_BASELINE_HOURS * 3600)
_MULTIPLIERS = [multiplier for _, multiplier in runtime_scaling._TIERS]
_CAP_SECONDS = _BASELINE_SECONDS * max(_MULTIPLIERS)
_MULTIPLIER_TEXT = "/".join(str(multiplier) for multiplier in _MULTIPLIERS)
_WATCHDOG_SECONDS = snowflake._MAX_EXECUTION_WAIT_SECONDS


def _size_text(size_bytes: float) -> str:
    """Render a finite tier boundary the way the docs write it: `10 GiB`, `1 TiB`."""
    if size_bytes % _TIB == 0:
        return f"{int(size_bytes // _TIB)} TiB"
    return f"{int(size_bytes // _GIB)} GiB"


_BOUNDARY_TEXTS = [_size_text(upper) for upper, _ in runtime_scaling._TIERS[:-1]]

# One phrase per tier, pairing the multiplier with its size band, so the README cannot attach
# the wrong multiplier to a band (or move a boundary) while every number still appears somewhere.
_README_TIER_PHRASES = [
    f"x{multiplier} under {boundary}"
    for (_, multiplier), boundary in zip(runtime_scaling._TIERS[:-1], _BOUNDARY_TEXTS, strict=True)
] + [f"x{_MULTIPLIERS[-1]} from {_BOUNDARY_TEXTS[-1]} up"]
_DOCSTRING_TIER_PHRASE = (
    f"multiplied by {_MULTIPLIER_TEXT} for scans "
    + ", ".join(f"under {boundary}" for boundary in _BOUNDARY_TEXTS)
    + f" and {_BOUNDARY_TEXTS[-1]} or more"
)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _known_limitation_bullets() -> list[str]:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("\n## Known limitations\n", 1)[1].split("\n## More docs", 1)[0]
    return [_flat(bullet) for bullet in re.split(r"\n- ", "\n" + section) if bullet.strip()]


def _bullet(marker: str) -> str:
    matches = [bullet for bullet in _known_limitation_bullets() if bullet.startswith(marker)]
    assert len(matches) == 1, f"expected exactly one Known limitations bullet starting {marker!r}"
    return matches[0]


# --- the code side: floor, monotone, cap -------------------------------------------------


def test_snowflake_and_databricks_use_the_same_baseline() -> None:
    assert databricks._ASSUMED_RUNTIME_HOURS == snowflake._ASSUMED_RUNTIME_HOURS
    assert _BASELINE_SECONDS == 30


def test_watchdog_is_the_documented_120_seconds() -> None:
    assert _WATCHDOG_SECONDS == 120
    assert databricks._MAX_EXECUTION_WAIT_SECONDS == _WATCHDOG_SECONDS


def test_tier_table_is_the_documented_one() -> None:
    assert _MULTIPLIERS == [1, 4, 20, 100]
    assert _BOUNDARY_TEXTS == ["10 GiB", "100 GiB", "1 TiB"]


@pytest.mark.parametrize(
    ("size_bytes", "multiplier"),
    [
        (None, 1),
        (0, 1),
        (10 * _GIB - 1, 1),
        (10 * _GIB, 4),
        (100 * _GIB - 1, 4),
        (100 * _GIB, 20),
        (_TIB - 1, 20),
        (_TIB, 100),
    ],
)
def test_multiplier_at_each_tier_boundary(size_bytes: int | None, multiplier: int) -> None:
    scaled = scale_runtime_hours(size_bytes, baseline_hours=_BASELINE_HOURS)
    assert scaled == pytest.approx(_BASELINE_HOURS * multiplier)


def test_runtime_is_never_below_the_baseline_floor() -> None:
    sizes = [None, 0, 1, 10 * _GIB, 100 * _GIB, _TIB, 5 * _TIB, 2**62]
    for size_bytes in sizes:
        assert scale_runtime_hours(size_bytes, baseline_hours=_BASELINE_HOURS) >= _BASELINE_HOURS


def test_runtime_is_monotone_non_decreasing_in_bytes() -> None:
    sizes = [0, 1, 10 * _GIB - 1, 10 * _GIB, 100 * _GIB - 1, 100 * _GIB, _TIB - 1, _TIB, 2**62]
    scaled = [scale_runtime_hours(size, baseline_hours=_BASELINE_HOURS) for size in sizes]
    assert scaled == sorted(scaled)


def test_runtime_is_capped_at_3000_seconds_however_large_the_scan() -> None:
    assert _CAP_SECONDS == 3000
    for size_bytes in (_TIB, 50 * _TIB, 2**62):
        seconds = scale_runtime_hours(size_bytes, baseline_hours=_BASELINE_HOURS) * 3600
        assert seconds == pytest.approx(_CAP_SECONDS)


def test_runtime_depends_on_bytes_only() -> None:
    # The signature is the whole input: no SQL text, plan shape or row count reaches it, which
    # is why a compute-heavy small scan is priced at the floor.
    parameters = list(inspect.signature(scale_runtime_hours).parameters)
    assert parameters == ["estimated_bytes", "baseline_hours"]


# --- the docs side: module docstring ------------------------------------------------------


def test_module_docstring_states_the_numbers_the_code_uses() -> None:
    doc = _flat(runtime_scaling.__doc__ or "")
    assert f"passes a {_BASELINE_SECONDS} s baseline" in doc
    assert _DOCSTRING_TIER_PHRASE in doc
    assert f"never below {_BASELINE_SECONDS} s and never above {_CAP_SECONDS} s" in doc
    assert f"priced at the {_BASELINE_SECONDS} s floor" in doc
    assert f"priced at {_CAP_SECONDS} s." in doc


def test_module_docstring_does_not_claim_over_estimation() -> None:
    doc = _flat(runtime_scaling.__doc__ or "").lower()
    assert "never under" not in doc
    assert "over-estimating" not in doc
    assert "not an upper bound" in doc


# --- the docs side: README Known limitations ----------------------------------------------


def test_readme_never_claims_the_fallback_over_estimates() -> None:
    readme = _flat(README.read_text(encoding="utf-8")).lower()
    assert "never under" not in readme
    assert "biases toward over" not in readme


def test_readme_runtime_baseline_bullet_matches_the_code() -> None:
    bullet = _bullet("Dollar figures rest on a fixed runtime placeholder")
    assert f"{_BASELINE_SECONDS}-second baseline constant" in bullet
    for phrase in _README_TIER_PHRASES:
        assert phrase in bullet
    assert f"({_MULTIPLIER_TEXT})" in bullet
    assert f"never below {_BASELINE_SECONDS} seconds" in bullet
    assert f"never above {_CAP_SECONDS} seconds" in bullet
    assert f"priced at {_BASELINE_SECONDS} seconds" in bullet
    assert f"priced at {_CAP_SECONDS} seconds at most" in bullet
    assert "not an upper bound" in bullet
    # README "What makes this different" and the tool description call UPPER_BOUND a "real cap";
    # this bullet is where the byte-only scope of that wording is stated.
    assert "`UPPER_BOUND`; that describes the byte figure" in bullet
    assert "applies to bytes only" in bullet
    assert "Databricks has no calibration" in bullet


def test_readme_cap_enforcement_bullet_matches_the_code() -> None:
    bullet = _bullet("Dollar and byte caps are enforced by the provider only on BigQuery")
    # The structural fact: only the BigQuery executor receives a byte bound to hand the provider.
    assert "max_bytes_billed" in inspect.signature(bigquery.execute_bounded).parameters
    for engine in (snowflake, databricks):
        assert "max_bytes_billed" not in inspect.signature(engine.execute_bounded).parameters
    assert snowflake._MAX_EXECUTION_WAIT_SECONDS == databricks._MAX_EXECUTION_WAIT_SECONDS
    assert "maximumBytesBilled" in bullet
    assert "Snowflake and Databricks" in bullet
    # Anchored on the clause itself: the bullet also cross-references "the 120-second bullet",
    # and "20-second" is a substring of "120-second".
    assert re.search(rf"only hard stop is the {_WATCHDOG_SECONDS}-second wall-clock limit", bullet)
    assert f"can therefore run for up to {_WATCHDOG_SECONDS} seconds" in bullet


def test_readme_list_price_bullet_matches_the_code() -> None:
    bullet = _bullet("Prices are US list prices on every engine")
    assert f"${ON_DEMAND_USD_PER_TIB}/TiB" in bullet
    assert f"${SERVERLESS_USD_PER_DBU:.2f}/DBU" in bullet
    assert "BigQuery" in bullet
    assert "Snowflake" in bullet


def test_readme_databricks_live_bullet_matches_the_repo() -> None:
    bullet = _bullet("Databricks has no live test job and no live `EXPLAIN COST` capture")
    # The repo's own record: no automated live job, and a hand run on 2026-09-14 (commit dbde8be)
    # that the bullet must keep citing rather than claiming Databricks was never run live.
    assert "2026-09-14" in bullet
    assert "has not been run against a live warehouse" not in bullet
    assert "fakes only" not in bullet
    # Adding a live job or module makes this bullet false: whoever adds one must reword it.
    live_modules = sorted(path.name for path in INTEGRATION_DIR.glob("test_*databricks*"))
    assert live_modules == []
    assert "databricks" not in INTEGRATION_WORKFLOW.read_text(encoding="utf-8").lower()


def test_changelog_does_not_claim_databricks_was_never_run_live() -> None:
    unreleased = CHANGELOG.read_text(encoding="utf-8").split("\n## [", 2)[1]
    assert "never been run" not in unreleased
    assert "has not been run against a live warehouse" not in _flat(unreleased)
