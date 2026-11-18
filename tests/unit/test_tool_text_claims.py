"""Pin what the tool descriptions and describe_engine_capabilities say to what the code does.

Tool-facing text is what an LLM client reads before it decides how far to trust an estimate or
a cap, so it has to say what the code does. PR #145 found that the fixed runtime fallback is a
floored, monotone, capped, bytes-only step function that can UNDER-estimate, that only BigQuery
enforces the caps provider-side, that prices are US list prices and that Databricks has no live
test job, and fixed the runtime_scaling docstring and the README then; the tool-facing text said
"UPPER_BOUND (real cap, may overstate)" until now. Every claim below is read from the real tool
listing, the real known_gaps and the real engine modules, so changing the text or the code alone
fails a test (the same two-sided approach as test_runtime_scaling_claims.py, which pins the
README side).
"""

import asyncio
import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.engines import bigquery, databricks, snowflake
from cost_guard_mcp.engines.bigquery import cost_cap_to_max_bytes_billed
from cost_guard_mcp.engines.bigquery_estimate_rules import BQ_ACCURACY_TO_TIER
from cost_guard_mcp.pricing import runtime_scaling, snowflake_pricing
from cost_guard_mcp.pricing.bigquery_pricing import ON_DEMAND_USD_PER_TIB
from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU
from cost_guard_mcp.server import mcp
from cost_guard_mcp.tools.describe_engine_capabilities import describe_engine_capabilities
from cost_guard_mcp.tools.run_query_bounded import run_query_bounded
from cost_guard_mcp.types import AccuracyTier, CostEstimate, EngineCapabilities

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
RELEASE_NOTES_DIR = REPO_ROOT / "changelog"
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
INTEGRATION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "integration.yml"

_ENGINES = ("bigquery", "snowflake", "databricks")
_DISPLAY = {"bigquery": "BigQuery", "snowflake": "Snowflake", "databricks": "Databricks"}

_GIB = 1024**3
_TIB = 1024**4
_MIB = 1024**2
_BASELINE_SECONDS = round(snowflake._ASSUMED_RUNTIME_HOURS * 3600)
_MULTIPLIERS = [multiplier for _, multiplier in runtime_scaling._TIERS]
_CAP_SECONDS = _BASELINE_SECONDS * max(_MULTIPLIERS)
_WATCHDOG_SECONDS = snowflake._MAX_EXECUTION_WAIT_SECONDS


def _size_text(size_bytes: float) -> str:
    return (
        f"{int(size_bytes // _TIB)} TiB"
        if size_bytes % _TIB == 0
        else f"{int(size_bytes // _GIB)} GiB"
    )


_BOUNDARIES = [_size_text(upper) for upper, _ in runtime_scaling._TIERS[:-1]]
# The tier phrases as the known_gaps write them, built from the code's own tier table.
_TIER_PHRASES = [
    f"x{multiplier} under {boundary}"
    for (_, multiplier), boundary in zip(runtime_scaling._TIERS[:-1], _BOUNDARIES, strict=True)
] + [f"x{_MULTIPLIERS[-1]} from {_BOUNDARIES[-1]} up"]

_PLACEHOLDER_SENTENCE = f"fixed {_BASELINE_SECONDS}-second runtime placeholder"
_FLOOR_AND_CAP = f"never below {_BASELINE_SECONDS} s, never above {_CAP_SECONDS} s"
_DIRECTIONAL = "directional, not as an upper bound or a cap on spend"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _description(name: str) -> str:
    tools = asyncio.run(mcp.list_tools())
    return _flat(next(tool.description for tool in tools if tool.name == name))


def _gaps(engine: str) -> list[str]:
    return [_flat(gap) for gap in describe_engine_capabilities(engine).known_gaps]


def _gap(engine: str, marker: str) -> str:
    matches = [gap for gap in _gaps(engine) if marker in gap]
    assert len(matches) == 1, f"expected exactly one {engine} known_gap with {marker!r}"
    return matches[0]


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _known_limitation_bullets() -> list[str]:
    section = _readme().split("\n## Known limitations\n", 1)[1].split("\n## More docs", 1)[0]
    return [_flat(bullet) for bullet in re.split(r"\n- ", "\n" + section) if bullet.strip()]


def _limitation(marker: str) -> str:
    matches = [b for b in _known_limitation_bullets() if b.startswith(marker)]
    assert len(matches) == 1, f"expected exactly one Known limitations bullet starting {marker!r}"
    return matches[0]


def _tiers_named_in(*functions) -> set[str]:
    source = "\n".join(inspect.getsource(function) for function in functions)
    return set(re.findall(r"AccuracyTier\.(\w+)", source))


def _tiers_each_engine_can_emit() -> dict[str, set[str]]:
    return {
        "bigquery": _tiers_named_in(bigquery.dry_run)
        | {tier.value for tier in BQ_ACCURACY_TO_TIER.values()},
        "snowflake": _tiers_named_in(snowflake.explain_estimate),
        "databricks": _tiers_named_in(databricks.explain_estimate, databricks._refused_estimate),
    }


# --- estimate_query_cost: what each accuracy tier covers -------------------------------------


def test_upper_bound_is_emitted_by_bigquery_and_snowflake_and_never_by_databricks():
    emitted = _tiers_each_engine_can_emit()
    assert {e for e, tiers in emitted.items() if "UPPER_BOUND" in tiers} == {
        "bigquery",
        "snowflake",
    }
    assert emitted["databricks"] == {"HEURISTIC"}
    # BigQuery reaches UPPER_BOUND from its own UPPER_BOUND and from UNKNOWN.
    upper = {raw for raw, tier in BQ_ACCURACY_TO_TIER.items() if tier is AccuracyTier.UPPER_BOUND}
    assert upper == {"UPPER_BOUND", "UNKNOWN"}


@pytest.mark.parametrize("accuracy", ["UNKNOWN", "SOMETHING_BIGQUERY_ADDS_LATER"])
@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_bigquery_labels_unknown_and_unrecognised_accuracy_upper_bound(
    mock_bq_module, _mock_capacity, accuracy
):
    job = MagicMock()
    job.total_bytes_processed = 500
    job._properties = {"statistics": {"query": {"totalBytesProcessedAccuracy": accuracy}}}
    job.referenced_tables = []
    job.location = "US"
    client = MagicMock()
    client.project = "my-project"
    client.query.return_value = job
    mock_bq_module.Client.return_value = client

    assert bigquery.dry_run("SELECT * FROM t").accuracy_tier is AccuracyTier.UPPER_BOUND


def test_estimate_description_names_exactly_the_engines_that_can_emit_upper_bound():
    description = _description("estimate_query_cost")
    segment = description.split("UPPER_BOUND (", 1)[1].split("HEURISTIC (", 1)[0]
    emitters = {e for e, tiers in _tiers_each_engine_can_emit().items() if "UPPER_BOUND" in tiers}
    for engine in _ENGINES:
        assert (_DISPLAY[engine] in segment) == (engine in emitters), engine
    # BigQuery labels UNKNOWN and unrecognised accuracy the same way without a bound behind it.
    assert "UNKNOWN" in segment and "unrecognised" in segment
    assert "without BigQuery vouching for it" in segment
    assert "may overstate" in segment


def test_estimate_description_scopes_the_tier_to_the_byte_figure_not_the_dollar_figure():
    description = _description("estimate_query_cost")
    assert "real cap" not in description
    assert "what the BYTE figure guarantees, not the dollar figure" in description
    assert "PRECISE (BigQuery's own dry-run byte count)" in description
    assert "HEURISTIC (rough, no bound either way" in description


def test_estimate_description_says_the_dollar_figure_can_under_estimate():
    description = _description("estimate_query_cost")
    assert _PLACEHOLDER_SENTENCE in description
    assert _FLOOR_AND_CAP in description
    assert "UNDER-estimate" in description
    assert _DIRECTIONAL in description
    assert "exact repeat of the same SQL text" in description
    assert "Dollar figures use US list prices on every engine" in description


def test_the_dollar_figure_is_built_from_history_on_snowflake_only_else_the_byte_tiers():
    snowflake_runtime = inspect.getsource(snowflake._runtime_for)
    assert "_lookup_historical_runtime" in snowflake_runtime
    assert "scale_runtime_hours" in snowflake_runtime
    databricks_estimate = inspect.getsource(databricks.explain_estimate)
    assert "scale_runtime_hours" in databricks_estimate
    assert not hasattr(databricks, "_lookup_historical_runtime")
    assert "history" not in databricks_estimate.lower()
    assert "_lookup_historical_runtime" not in inspect.getsource(databricks)


@pytest.mark.parametrize(
    "estimate", [bigquery.dry_run, snowflake.explain_estimate, databricks.explain_estimate]
)
def test_no_estimate_function_takes_a_region_so_prices_cannot_follow_the_region(estimate):
    names = list(inspect.signature(estimate).parameters)
    assert not [name for name in names if "region" in name or "location" in name], names


# --- run_query_bounded: what enforces the caps -----------------------------------------------


def test_only_the_bigquery_executor_is_handed_a_provider_side_limit():
    assert "max_bytes_billed" in inspect.signature(bigquery.execute_bounded).parameters
    assert "maximum_bytes_billed" in inspect.getsource(bigquery.execute_bounded)
    for engine in (snowflake, databricks):
        names = list(inspect.signature(engine.execute_bounded).parameters)
        assert not [n for n in names if "bytes" in n or "cost" in n or "usd" in n], names


def test_snowflake_and_databricks_hard_stop_is_the_watchdog_plus_a_statement_timeout():
    assert _WATCHDOG_SECONDS == 120
    assert databricks._MAX_EXECUTION_WAIT_SECONDS == _WATCHDOG_SECONDS
    snowflake_run = inspect.getsource(snowflake.execute_bounded)
    assert "statement_timeout_seconds=_MAX_EXECUTION_WAIT_SECONDS" in snowflake_run
    assert "_cancel_query(" in snowflake_run
    databricks_run = inspect.getsource(databricks.execute_bounded)
    assert "statement_timeout_seconds=" in databricks_run
    assert "cur.cancel()" in databricks_run


def test_run_query_bounded_description_states_the_enforcement_model():
    description = _description("run_query_bounded")
    assert "only BigQuery also enforces them itself" in description
    assert "the smaller of max_bytes_billed and a limit derived from a finite" in description
    assert "attached to the job as maximumBytesBilled" in description
    assert "On Snowflake and Databricks no provider-side spend limit is set" in description
    assert "the estimate can under-read" in description
    assert f"the only hard stop the server sets is the {_WATCHDOG_SECONDS} s limit" in description
    assert "a watchdog cancel plus a server-side statement timeout" in description
    assert "refused, never ignored" in description


def _estimate(engine, **fields):
    return CostEstimate(engine=engine, accuracy_tier=AccuracyTier.HEURISTIC, **fields)


@pytest.mark.parametrize(
    ("caller_bytes_cap", "expected"),
    [(10**6, 10**6), (10**13, cost_cap_to_max_bytes_billed(1.0))],
    ids=["caller_byte_cap_is_smaller", "derived_limit_is_smaller"],
)
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_bigquery_is_handed_the_smaller_of_the_byte_cap_and_the_derived_limit(
    mock_estimate, mock_engine, caller_bytes_cap, expected
):
    mock_estimate.return_value = _estimate(
        "bigquery", estimated_bytes=1000, estimated_cost_usd=0.01
    )
    mock_engine.execute_bounded.return_value = ([], 0, False)

    run_query_bounded(
        "bigquery", "SELECT 1", max_bytes_billed=caller_bytes_cap, max_estimated_cost_usd=1.0
    )

    assert mock_engine.execute_bounded.call_args.kwargs["max_bytes_billed"] == expected


def test_the_derived_limit_rounds_to_mib_steps_with_a_ten_mib_minimum():
    assert cost_cap_to_max_bytes_billed(1.0) % _MIB == 0
    assert cost_cap_to_max_bytes_billed(1e-9) == bigquery._MIN_MAXIMUM_BYTES_BILLED == 10 * _MIB


@pytest.mark.parametrize("engine", ["snowflake", "databricks"])
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_snowflake_and_databricks_executors_are_handed_no_byte_or_dollar_limit(
    mock_estimate, mock_snowflake, mock_databricks, engine
):
    mock_estimate.return_value = _estimate(engine, estimated_bytes=1000, estimated_cost_usd=0.01)
    executor = {"snowflake": mock_snowflake, "databricks": mock_databricks}[engine].execute_bounded
    executor.return_value = ([], 0, False)

    run_query_bounded(
        engine, "SELECT 1", max_bytes_billed=10**6, max_estimated_cost_usd=1.0, max_rows=5
    )

    call = executor.call_args
    assert call.args == ("SELECT 1",)
    assert not [k for k in call.kwargs if "bytes" in k or "cost" in k or "usd" in k], call.kwargs


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    ("caps", "estimate_fields"),
    [
        ({"max_estimated_cost_usd": 1.0}, {"estimated_bytes": 1000}),
        ({"max_bytes_billed": 10**6}, {"estimated_cost_usd": 0.01}),
    ],
    ids=["dollar_cap_without_a_dollar_figure", "byte_cap_without_a_byte_figure"],
)
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_a_requested_cap_with_no_usable_estimate_refuses_before_anything_is_submitted(
    mock_estimate, mock_bigquery, mock_snowflake, mock_databricks, engine, caps, estimate_fields
):
    mock_estimate.return_value = _estimate(engine, **estimate_fields)

    result = run_query_bounded(engine, "SELECT 1", **caps)

    assert result.status == "refused"
    assert result.executed is False
    for executor in (mock_bigquery, mock_snowflake, mock_databricks):
        executor.execute_bounded.assert_not_called()


# --- describe_engine_capabilities.known_gaps ------------------------------------------------


def test_the_capabilities_schema_is_unchanged_known_gaps_is_still_a_list_of_strings():
    assert set(EngineCapabilities.model_fields) == {
        "engine",
        "supports_precise_bytes",
        "supports_dollar_estimate",
        "default_accuracy_tier",
        "known_gaps",
    }
    assert EngineCapabilities.model_fields["known_gaps"].annotation == list[str]


@pytest.mark.parametrize("engine", ["snowflake", "databricks"])
def test_runtime_placeholder_gap_matches_the_code(engine):
    gap = _gap(engine, "runtime placeholder")
    assert _PLACEHOLDER_SENTENCE in gap
    for phrase in _TIER_PHRASES:
        assert phrase in gap, phrase
    assert _FLOOR_AND_CAP in gap
    assert "It cannot see compute cost, so it can under-estimate" in gap
    assert _DIRECTIONAL in gap
    # The old text called the placeholder-based figure "a tight bound", i.e. a loose bound.
    assert "tight bound" not in " ".join(_gaps(engine))


def test_snowflake_gap_scopes_upper_bound_to_bytes_and_names_the_history_exception():
    gap = _gap("snowflake", "runtime placeholder")
    assert (
        "UPPER_BOUND describes the byte figure (EXPLAIN's bytesAssigned) only, "
        "not the dollar figure" in gap
    )
    assert "exact repeat of the same SQL text" in gap


def test_databricks_gap_says_there_is_no_history_calibration():
    assert "with no query-history calibration" in _gap("databricks", "runtime placeholder")


def test_bigquery_gap_says_only_bigquery_enforces_the_caps_itself():
    gap = _gap("bigquery", "Caps are enforced by BigQuery itself")
    assert "the only engine where they are" in gap
    assert "the smaller of max_bytes_billed and a limit derived from a finite" in gap
    assert "maximumBytesBilled" in gap
    assert "1 MiB steps with a 10 MiB minimum per referenced table" in gap


@pytest.mark.parametrize("engine", ["snowflake", "databricks"])
def test_snowflake_and_databricks_gap_says_caps_are_checked_against_the_estimate_only(engine):
    gap = _gap(engine, "Caps are checked against the estimate only")
    assert f"{_DISPLAY[engine]} enforces none of them" in gap
    assert "no provider-side spend limit is set" in gap
    assert "a requested cap with no usable estimate refuses" in gap
    assert f"the only hard stop the server sets is the {_WATCHDOG_SECONDS}-second limit" in gap
    assert f"can therefore run for up to {_WATCHDOG_SECONDS} seconds" in gap


def test_every_engine_gap_says_prices_are_us_list_prices_and_names_the_constants():
    bigquery_gap = _gap("bigquery", "Prices are US list prices")
    assert f"${ON_DEMAND_USD_PER_TIB}/TiB" in bigquery_gap
    snowflake_gap = _gap("snowflake", "Prices are US list prices")
    regions = "AWS US East / Azure East US / GCP us-central1"
    assert regions in snowflake_gap
    assert regions in (snowflake_pricing.__doc__ or "")
    databricks_gap = _gap("databricks", "Prices are US list prices")
    assert f"${SERVERLESS_USD_PER_DBU:.2f}/DBU" in databricks_gap
    for gap in (bigquery_gap, snowflake_gap, databricks_gap):
        assert "a max_estimated_cost_usd cap compares against that figure" in gap


def test_databricks_live_gap_matches_the_repo_and_the_readme():
    gap = _gap("databricks", "no live test job")
    # Adding a live job or module makes the gap false: whoever adds one must reword it.
    assert list(INTEGRATION_DIR.glob("test_*databricks*")) == []
    assert "databricks" not in INTEGRATION_WORKFLOW.read_text(encoding="utf-8").lower()
    assert (RELEASE_NOTES_DIR / "v0.4.0.md").is_file()
    assert "nothing added from 0.4.0 on has a recorded live run" in gap
    assert "no output recorded in the repo" in gap
    bullet = _limitation("Databricks has no live test job and no live `EXPLAIN COST` capture")
    gap_dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", gap))
    assert gap_dates == {"2026-09-14", "2026-09-18"}
    assert gap_dates <= set(re.findall(r"\d{4}-\d{2}-\d{2}", bullet))
    assert "has not been run against a live warehouse" not in gap


# --- README and source comments say the same -----------------------------------------------


def test_readme_never_calls_upper_bound_a_real_cap():
    assert "real cap" not in _flat(_readme()).lower()


def test_readme_pointer_to_the_tool_description_stays_true():
    bullet = _limitation("Dollar figures rest on a fixed runtime placeholder")
    assert "the `UPPER_BOUND` wording in the `estimate_query_cost` tool description" in bullet
    assert "and the tool description says so" in bullet
    assert "what the BYTE figure guarantees, not the dollar figure" in _description(
        "estimate_query_cost"
    )


def _readme_line(prefix: str) -> str:
    matches = [line for line in _readme().splitlines() if line.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one README line starting {prefix!r}"
    return _flat(matches[0])


def test_readme_accuracy_tier_bullet_scopes_the_tier_to_the_byte_figure():
    bullet = _readme_line("- **Every cost estimate discloses its accuracy tier**")
    assert (
        "`UPPER_BOUND` (Snowflake `EXPLAIN`, and BigQuery when it reports `UPPER_BOUND` "
        "or `UNKNOWN`)" in bullet
    )
    assert "The tier describes the byte figure" in bullet
    assert "can under-estimate" in bullet


def test_readme_run_query_bounded_bullet_states_the_enforcement_model():
    bullet = _readme_line("- `run_query_bounded(engine, sql,")
    assert "only BigQuery also enforces them provider-side (`maximumBytesBilled`)" in bullet
    assert f"the {_WATCHDOG_SECONDS}-second limit is the only hard stop" in bullet
    assert "a requested cap with no usable estimate refuses" in bullet


def test_readme_estimate_bullet_says_the_tier_describes_the_byte_figure():
    bullet = _readme_line("- `estimate_query_cost(engine, sql,")
    assert "tagged with its accuracy tier, which describes the byte figure" in bullet


@pytest.mark.parametrize("module", [snowflake, databricks], ids=["snowflake", "databricks"])
def test_runtime_placeholder_comment_does_not_call_the_fallback_conservative(module):
    lines = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("_ASSUMED_RUNTIME_HOURS ="))
    block = []
    for line in reversed(lines[:index]):
        if not line.startswith("#"):
            break
        block.append(line.lstrip("# ").rstrip())
    comment = _flat(" ".join(reversed(block)))
    assert "deliberately conservative" not in comment
    assert "not conservative" in comment
    assert "pricing/runtime_scaling.py" in comment
    assert "under-estimate" in comment


def test_changelog_records_the_tool_text_change_with_no_behaviour_change():
    unreleased = CHANGELOG.read_text(encoding="utf-8").split("\n## [", 2)[1]
    entries = [_flat(b) for b in re.split(r"\n- ", unreleased) if "tool descriptions" in b]
    assert len(entries) == 1
    assert "describe_engine_capabilities" in entries[0]
    assert "No behaviour change." in entries[0]


# --- the provider-side enforcement claim is scoped, and names what is compared ---------------


def test_run_query_bounded_description_scopes_provider_enforcement_to_on_demand():
    # Google frames maximum bytes billed as a control "when using the on-demand pricing model"
    # (docs.cloud.google.com/bigquery/docs/best-practices-costs, read 2026-11-18) and is silent on
    # capacity billing; README Known limitations and DECISIONS.md #16 carry the same scope.
    description = _description("run_query_bounded")
    assert (
        "only BigQuery also enforces them itself, and only on an on-demand project (" in description
    )


def test_run_query_bounded_description_names_the_two_caps_compared_with_the_estimate():
    description = _description("run_query_bounded")
    assert (
        "The max_bytes_billed and max_estimated_cost_usd caps are checked against that estimate "
        "before the query is submitted" in description
    )
    # max_rows is a bound of the call but is never compared with the estimate, and the estimate
    # itself runs EXPLAIN and metadata statements on the warehouse (allow_warehouse_wake=True).
    assert "The bounds are checked" not in description
    assert "before anything is submitted" not in description


def test_the_120_second_limit_is_the_only_hard_stop_on_run_time_not_on_rows_or_size():
    assert (
        f"the only hard stop the server sets is the {_WATCHDOG_SECONDS} s limit on how long it runs"
        in _description("run_query_bounded")
    )
    for engine in ("snowflake", "databricks"):
        gap = _gap(engine, "Caps are checked against the estimate only")
        assert (
            f"the only hard stop the server sets is the {_WATCHDOG_SECONDS}-second limit "
            "on how long it runs" in gap
        )


def test_bigquery_gap_scopes_provider_enforcement_to_on_demand_and_leaves_capacity_unverified():
    gap = _gap("bigquery", "Caps are enforced by BigQuery itself")
    assert "the only engine where they are, and only on an on-demand project:" in gap
    assert (
        "On a capacity-billed project there is no dollar figure, so a dollar cap refuses; a "
        "max_bytes_billed cap is still attached to the job, but whether BigQuery enforces it "
        "there is not verified." in gap
    )


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_a_bigquery_byte_cap_is_attached_to_the_job_whatever_the_billing_model(
    mock_estimate, mock_engine
):
    # A capacity-billed dry run carries bytes and no dollar figure (bigquery.dry_run). Nothing on
    # the execution path consults the billing model, so the job carries the cap either way; the
    # known_gap therefore calls enforcement on a capacity-billed project unverified, not absent.
    mock_estimate.return_value = _estimate("bigquery", estimated_bytes=1000)
    mock_engine.execute_bounded.return_value = ([], 0, False)

    run_query_bounded("bigquery", "SELECT 1", max_bytes_billed=10**6)

    assert mock_engine.execute_bounded.call_args.kwargs["max_bytes_billed"] == 10**6
    assert "is_capacity_billed" not in inspect.getsource(bigquery.execute_bounded)


@pytest.mark.parametrize("engine", _ENGINES)
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_max_rows_is_never_compared_with_the_estimate_on_any_engine(
    mock_estimate, mock_bigquery, mock_snowflake, mock_databricks, engine
):
    mock_estimate.return_value = _estimate(engine, estimated_bytes=10**12, estimated_cost_usd=500.0)
    executor = {
        "bigquery": mock_bigquery,
        "snowflake": mock_snowflake,
        "databricks": mock_databricks,
    }[engine].execute_bounded
    executor.return_value = ([], 0, False)

    result = run_query_bounded(engine, "SELECT 1", max_rows=1)

    assert result.status == "ok"
    assert executor.call_args.kwargs["max_rows"] == 1


def test_readme_run_query_bounded_bullet_scopes_enforcement_and_names_the_two_caps():
    bullet = _readme_line("- `run_query_bounded(engine, sql,")
    assert (
        "The `max_bytes_billed` and `max_estimated_cost_usd` caps are checked against the "
        "estimate before the query is submitted" in bullet
    )
    assert (
        "only BigQuery also enforces them provider-side (`maximumBytesBilled`), on an "
        "on-demand project" in bullet
    )
    assert (
        f"the {_WATCHDOG_SECONDS}-second limit is the only hard stop on how long the query runs"
        in bullet
    )
    assert "The bounds are checked" not in bullet
    assert "before anything is submitted" not in bullet


def test_changelog_scopes_provider_enforcement_to_an_on_demand_project():
    unreleased = CHANGELOG.read_text(encoding="utf-8").split("\n## [", 2)[1]
    entry = next(_flat(b) for b in re.split(r"\n- ", unreleased) if "tool descriptions" in b)
    assert "only BigQuery enforces the caps provider-side (on an on-demand project)" in entry
