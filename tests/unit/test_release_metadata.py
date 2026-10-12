"""Repository-level release and supply-chain invariants.

Unlike the rest of the unit suite these tests read the repository's own metadata files
(pyproject.toml, uv.lock, .pre-commit-config.yaml and the GitHub Actions workflows), not
the package under test. They exist so that the pins which bind what executes during a
release build can only move through a reviewed diff: Dependabot bumps neither
``[build-system].requires`` nor a ``setup-uv`` ``with: version:`` input, and pre-commit.ci's
autoupdate moves the uv-pre-commit ``rev`` on its own - these tests make the three move
together or fail CI (see DECISIONS.md #11).

No PyYAML is installed in this project, so the workflow files are scanned line by line;
tomllib (stdlib) parses pyproject.toml and uv.lock.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
UV_LOCK_PATH = REPO_ROOT / "uv.lock"
PRE_COMMIT_CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"

_SETUP_UV_USES = "uses: astral-sh/setup-uv@"
_UV_PRE_COMMIT_REPO = "repo: https://github.com/astral-sh/uv-pre-commit"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _uv_lock() -> dict:
    return tomllib.loads(UV_LOCK_PATH.read_text(encoding="utf-8"))


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert files, f"no workflow files found under {WORKFLOWS_DIR}"
    return files


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _step_blocks(workflow_text: str, marker: str) -> list[list[str]]:
    """Return the lines of every ``steps:`` entry whose text contains ``marker``.

    A step starts at the ``- `` that opens it and ends just before the next sibling ``- ``
    at the same indentation or before any line indented less than the step itself.
    """
    lines = workflow_text.splitlines()
    blocks: list[list[str]] = []
    for index, line in enumerate(lines):
        if marker not in line:
            continue
        start = index
        while not lines[start].lstrip().startswith("- "):
            start -= 1
        step_indent = _indent_of(lines[start])
        end = start + 1
        while end < len(lines):
            candidate = lines[end]
            stripped = candidate.strip()
            if stripped and not stripped.startswith("#"):
                candidate_indent = _indent_of(candidate)
                is_sibling = candidate_indent == step_indent and stripped.startswith("- ")
                if candidate_indent < step_indent or is_sibling:
                    break
            end += 1
        blocks.append(lines[start:end])
    return blocks


_VERSION_INPUT_RE = re.compile(r"^\s+version:\s*\"?([^\"\s#]+)\"?\s*(#.*)?$")


def _pinned_uv_version(step_lines: list[str]) -> str | None:
    """The ``with: version:`` value of a setup-uv step, or None when it floats."""
    in_with_block = False
    for line in step_lines[1:]:
        stripped = line.strip()
        if stripped.startswith("with:"):
            in_with_block = True
            continue
        if in_with_block:
            match = _VERSION_INPUT_RE.match(line)
            if match:
                return match.group(1)
    return None


def _setup_uv_steps_by_file() -> dict[str, list[list[str]]]:
    return {
        path.name: _step_blocks(path.read_text(encoding="utf-8"), _SETUP_UV_USES)
        for path in _workflow_files()
    }


def _uv_pre_commit_rev() -> str:
    lines = PRE_COMMIT_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if _UV_PRE_COMMIT_REPO in line:
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if stripped.startswith("rev:"):
                    return stripped.removeprefix("rev:").strip().strip("\"'")
    raise AssertionError(
        "astral-sh/uv-pre-commit entry with a rev: not found in .pre-commit-config.yaml"
    )


# --- release-build-toolchain-unbound-hatchling-and-setup-uv-latest:pyproject.toml ---------


def test_build_backend_is_exactly_pinned():
    # Every PEP 517 backend requirement must name one exact version, and uv's
    # build-constraint-dependencies must carry the identical specifier so `uv build`,
    # `uv sync`'s editable build and the Docker build all resolve the same backend.
    pyproject = _pyproject()
    requires = pyproject["build-system"]["requires"]
    assert requires, "[build-system].requires is empty"
    for requirement in requires:
        assert "==" in requirement, (
            f"build backend requirement {requirement!r} floats; pin it with '=='"
        )
    hatchling = [requirement for requirement in requires if requirement.startswith("hatchling==")]
    assert len(hatchling) == 1, f"expected exactly one hatchling pin, got {requires!r}"
    build_constraints = pyproject["tool"]["uv"]["build-constraint-dependencies"]
    assert hatchling[0] in build_constraints, (
        f"[tool.uv].build-constraint-dependencies {build_constraints!r} does not carry "
        f"the [build-system] pin {hatchling[0]!r}"
    )


def test_uv_lock_records_the_build_constraint():
    # The lock is what `uv sync --frozen` (Dockerfile) and `uv build` consult; the pin must
    # be recorded there, not only in pyproject.toml, so a stale lock cannot drop it.
    lock = _uv_lock()
    assert "manifest" in lock, "uv.lock has no [manifest] table - run `uv lock`"
    constraints = lock["manifest"].get("build-constraints")
    assert constraints, "uv.lock [manifest] records no build-constraints"
    locked = {f"{entry['name']}{entry['specifier']}" for entry in constraints}
    declared = set(_pyproject()["tool"]["uv"]["build-constraint-dependencies"])
    assert locked == declared, f"uv.lock build-constraints {locked!r} != pyproject {declared!r}"


def test_every_setup_uv_step_pins_a_version():
    # One line-scan over every workflow: each `uses: astral-sh/setup-uv@...` step must carry
    # a `with: version:` input, otherwise the action resolves whatever uv is current at run
    # time - including inside the publish job that holds the PyPI OIDC token.
    steps_by_file = _setup_uv_steps_by_file()
    total = sum(len(steps) for steps in steps_by_file.values())
    assert total >= 7, f"expected at least the seven known setup-uv steps, found {total}"
    unpinned = [
        f"{name}: {step[0].strip()}"
        for name, steps in steps_by_file.items()
        for step in steps
        if _pinned_uv_version(step) is None
    ]
    assert unpinned == [], f"setup-uv steps without a `with: version:` input: {unpinned}"


def test_ci_uv_version_matches_lock_producer():
    # All workflows must pin one and the same uv version, and it must equal the
    # uv-pre-commit rev that produces uv.lock, so the lock producer and every consumer run
    # one resolver. pre-commit.ci autoupdate PRs therefore fail until the workflows are
    # bumped in the same PR - intended coupling.
    versions = {
        _pinned_uv_version(step) for steps in _setup_uv_steps_by_file().values() for step in steps
    }
    versions.discard(None)
    assert len(versions) == 1, f"workflows pin more than one uv version (or none): {versions!r}"
    (ci_version,) = versions
    assert ci_version == _uv_pre_commit_rev(), (
        f"workflows pin uv {ci_version} but .pre-commit-config.yaml's uv-pre-commit rev is "
        f"{_uv_pre_commit_rev()}"
    )
