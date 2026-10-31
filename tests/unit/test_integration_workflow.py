"""Policy pins for ``.github/workflows/integration.yml`` (DECISIONS.md #9, amended 2026-10-31).

The Snowflake free trial ended (error 390913) and the owner decided not to buy a paid plan for
now, so the weekly scheduled run must exercise BigQuery only. The Snowflake job stays in the
file so restoring it is one input flip (``include_snowflake=true`` on a manual dispatch), but it
must never again run from the schedule or from a bare dispatch. If the schedule ran it, every
Monday would go red against an account that no longer exists.

No PyYAML is installed in this project, so the workflow is scanned line by line, as in
``test_release_metadata.py`` and ``test_workflow_pins.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "integration.yml"

INPUT_NAME = "include_snowflake"
SNOWFLAKE_IF = f"github.event_name == 'workflow_dispatch' && inputs.{INPUT_NAME}"


def _lines() -> list[str]:
    return WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _children(lines: list[str], header: str, indent: int) -> list[str]:
    """Lines nested under the ``header`` key at ``indent`` spaces (comments and blanks dropped)."""
    start = next(
        (i for i, line in enumerate(lines) if _indent(line) == indent and line.strip() == header),
        None,
    )
    assert start is not None, f"{header!r} not found at indent {indent} in {WORKFLOW_PATH.name}"
    nested: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        if _indent(line) <= indent:
            break
        nested.append(line)
    return nested


def _job(name: str) -> list[str]:
    return _children(_children(_lines(), "jobs:", 0), f"{name}:", 2)


def _job_level_if(name: str) -> str | None:
    """The job's own ``if:`` expression (four-space indent: step-level ``if:`` sits deeper)."""
    for line in _job(name):
        if _indent(line) == 4 and line.strip().startswith("if:"):
            expression = line.strip().removeprefix("if:").strip()
            wrapped = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expression)
            return " ".join((wrapped.group(1) if wrapped else expression).split())
    return None


def test_workflow_dispatch_offers_a_boolean_include_snowflake_input_defaulting_to_false():
    dispatch = _children(_children(_lines(), "on:", 0), "workflow_dispatch:", 2)
    inputs = _children(dispatch, "inputs:", 4)
    spec = [line.strip() for line in _children(inputs, f"{INPUT_NAME}:", 6)]
    assert "type: boolean" in spec
    assert "default: false" in spec
    description = next(line for line in spec if line.startswith("description:"))
    assert "paid" in description.lower(), "the input must say a paid Snowflake account is needed"


def test_snowflake_job_runs_only_for_a_dispatch_that_asked_for_it():
    # Exact pin: no `||`, no `always()`, no `schedule`. Anything wider re-arms the Monday red run.
    assert _job_level_if("snowflake") == SNOWFLAKE_IF


def test_bigquery_job_is_unconditional_and_unaware_of_the_snowflake_input():
    block = "\n".join(_job("bigquery"))
    assert _job_level_if("bigquery") is None
    assert INPUT_NAME not in block
    assert "environment: live-integration" in block


def test_weekly_schedule_trigger_is_still_present():
    schedule = [line.strip() for line in _children(_children(_lines(), "on:", 0), "schedule:", 2)]
    assert any(re.fullmatch(r'- cron: "0 6 \* \* 1"(\s+#.*)?', line) for line in schedule)


def test_the_input_is_referenced_by_nothing_but_its_declaration_and_the_snowflake_gate():
    # Two mentions: the input declaration and the snowflake job's `if`. A third would mean the
    # flag has started steering something else (for example the BigQuery job).
    text = "\n".join(_lines())
    assert len(re.findall(rf"\b{INPUT_NAME}\b", text)) == 2
