"""Workflow invariants for the ``github/codeql-action`` family.

``init`` and ``analyze`` (codeql.yml) must run the same release or the CodeQL job fails, and
``upload-sarif`` (scorecard.yml) is versioned from the same repository. Dependabot opens one
pull request per action, so each of them alone leaves the workflows with mismatched pins and
red CI. These tests make the pins move together (same commit SHA, same version comment) and
make sure ``.github/dependabot.yml`` keeps asking for the three bumps as one grouped PR.

No PyYAML is installed in this project, so workflow and Dependabot files are scanned line by
line, as in ``test_release_metadata.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

CODEQL_ACTIONS = ("init", "analyze", "upload-sarif")

_CODEQL_USES_RE = re.compile(
    r"^\s*(?:-\s+)?uses:\s*github/codeql-action/(?P<action>[\w-]+)@(?P<ref>\S+)"
    r"(?:\s+#\s*(?P<comment>.*?))?\s*$"
)
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_VERSION_COMMENT_RE = re.compile(r"v\d+\.\d+\.\d+")


class CodeqlUse(NamedTuple):
    file: str
    line: int
    action: str
    ref: str
    comment: str | None


def _workflow_texts() -> dict[str, str]:
    files = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert files, f"no workflow files found under {WORKFLOWS_DIR}"
    return {path.name: path.read_text(encoding="utf-8") for path in files}


def _codeql_action_uses(workflow_texts: dict[str, str]) -> list[CodeqlUse]:
    """Every ``uses: github/codeql-action/<action>@<ref>  # <comment>`` line, in file order."""
    found = []
    for name, text in workflow_texts.items():
        for number, line in enumerate(text.splitlines(), start=1):
            match = _CODEQL_USES_RE.match(line)
            if match:
                found.append(
                    CodeqlUse(name, number, match["action"], match["ref"], match["comment"])
                )
    return found


def _lockstep_violations(uses: list[CodeqlUse]) -> list[str]:
    """Why these codeql-action pins are not one reviewed release; empty when they are."""
    problems = []
    for use in uses:
        where = f"{use.file}:{use.line} codeql-action/{use.action}"
        if not _FULL_SHA_RE.fullmatch(use.ref):
            problems.append(f"{where}: ref {use.ref!r} is not a full lowercase commit SHA")
        if use.comment is None or not _VERSION_COMMENT_RE.fullmatch(use.comment):
            problems.append(f"{where}: trailing comment {use.comment!r} is not a vX.Y.Z version")
    distinct = sorted({(use.ref, use.comment) for use in uses}, key=repr)
    if len(distinct) > 1:
        listing = "; ".join(f"{use.action}@{use.ref[:12]} ({use.comment})" for use in uses)
        problems.append(f"codeql-action pins differ across workflows: {listing}")
    return problems


def _workflow(action_pins: dict[str, str]) -> dict[str, str]:
    """A synthetic workflow set with the given ``action -> '<sha>  # <version>'`` pins."""
    steps = "\n".join(
        f"      - uses: github/codeql-action/{action}@{pin}" for action, pin in action_pins.items()
    )
    return {"synthetic.yml": f"jobs:\n  analyze:\n    steps:\n{steps}\n"}


_OLD_PIN = "a" * 40 + "  # v1.0.0"
_NEW_PIN = "b" * 40 + "  # v1.1.0"


def test_every_codeql_action_step_is_present_in_the_repo_workflows():
    # Floor for the lockstep test below: if a rename or refactor hid the steps from the scan,
    # the lockstep test would pass vacuously.
    found = {use.action for use in _codeql_action_uses(_workflow_texts())}
    assert set(CODEQL_ACTIONS) <= found, (
        f"missing codeql-action steps: {set(CODEQL_ACTIONS) - found}"
    )


def test_codeql_action_steps_share_one_sha_and_version_comment():
    violations = _lockstep_violations(_codeql_action_uses(_workflow_texts()))
    assert not violations, (
        "github/codeql-action/init, analyze and upload-sarif must be bumped together "
        "(same SHA, same version comment):\n" + "\n".join(violations)
    )


def test_lockstep_check_accepts_three_matching_pins():
    pins = dict.fromkeys(CODEQL_ACTIONS, _OLD_PIN)
    assert _lockstep_violations(_codeql_action_uses(_workflow(pins))) == []


def test_lockstep_check_rejects_a_single_bumped_action():
    # What a lone Dependabot PR produces: one of the three moves, the others stay behind.
    for bumped in CODEQL_ACTIONS:
        pins = {action: _OLD_PIN for action in CODEQL_ACTIONS}
        pins[bumped] = _NEW_PIN
        violations = _lockstep_violations(_codeql_action_uses(_workflow(pins)))
        assert any("differ across workflows" in message for message in violations), bumped


def test_lockstep_check_rejects_same_sha_with_a_stale_version_comment():
    pins = dict.fromkeys(CODEQL_ACTIONS, _OLD_PIN)
    pins["analyze"] = "a" * 40 + "  # v0.9.0"
    violations = _lockstep_violations(_codeql_action_uses(_workflow(pins)))
    assert any("differ across workflows" in message for message in violations)


def test_lockstep_check_rejects_a_tag_ref_and_a_missing_version_comment():
    pins = dict.fromkeys(CODEQL_ACTIONS, _OLD_PIN)
    pins["init"] = "v4"
    pins["analyze"] = "a" * 40
    violations = _lockstep_violations(_codeql_action_uses(_workflow(pins)))
    assert any("not a full lowercase commit SHA" in message for message in violations)
    assert any("not a vX.Y.Z version" in message for message in violations)


def test_lockstep_scan_ignores_commented_out_steps():
    texts = {"synthetic.yml": f"      # - uses: github/codeql-action/init@{_NEW_PIN}\n"}
    assert _codeql_action_uses(texts) == []
