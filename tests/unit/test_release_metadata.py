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

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
UV_LOCK_PATH = REPO_ROOT / "uv.lock"
PRE_COMMIT_CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"
RELEASE_WORKFLOW_PATH = WORKFLOWS_DIR / "release.yml"

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


def _job_block(workflow_text: str, job_name: str) -> list[str]:
    """The lines of ``jobs.<job_name>`` (keys under ``jobs:`` sit at two-space indentation)."""
    lines = workflow_text.splitlines()
    start = lines.index(f"  {job_name}:")
    end = start + 1
    while end < len(lines):
        candidate = lines[end]
        if candidate.strip() and _indent_of(candidate) <= 2:
            break
        end += 1
    return lines[start:end]


def _named_step(block: list[str], step_name: str) -> list[str]:
    steps = _step_blocks("\n".join(block), f"name: {step_name}")
    assert len(steps) == 1, f"expected exactly one step named {step_name!r}, found {len(steps)}"
    return steps[0]


def _step_run_script(step_lines: list[str]) -> str:
    """The dedented text of a step's ``run: |`` block, exactly as the runner would execute it."""
    for index, line in enumerate(step_lines):
        if line.strip() == "run: |":
            run_indent = _indent_of(line)
            body = []
            for following in step_lines[index + 1 :]:
                if following.strip() and _indent_of(following) <= run_indent:
                    break
                body.append(following)
            body_indent = min(_indent_of(item) for item in body if item.strip())
            return "\n".join(item[body_indent:] for item in body) + "\n"
    raise AssertionError("step has no `run: |` block")


def _hermetic_git_env(home: Path) -> dict[str, str]:
    # No host gitconfig (signing, hooks, default branch) may leak into the fixture repos.
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }


def _git(*args: str, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argv, fixture paths only
        [shutil.which("git") or "git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _run_step_script(
    script: str, *, cwd: Path, env: dict[str, str], scratch: Path
) -> subprocess.CompletedProcess[str]:
    # GitHub Actions writes a `run:` block to a file and executes it with
    # `bash --noprofile --norc -eo pipefail {0}`; mirror that exactly.
    script_path = scratch / "step.sh"
    script_path.write_text(script, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - the script is the repository's own workflow text
        [
            shutil.which("bash") or "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            str(script_path),
        ],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
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


# --- release-authority-tag-push-unbound-to-reviewed-main:.github/workflows/release.yml ------


def test_release_workflow_verifies_tag_is_on_main():
    # Text pins for the defence-in-depth step so a later simplification cannot silently
    # drop it: the build job checks out full history and runs merge-base --is-ancestor.
    build_job = _job_block(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"), "build")
    checkout_steps = _step_blocks("\n".join(build_job), "uses: actions/checkout@")
    assert len(checkout_steps) == 1
    assert any(line.strip() == "fetch-depth: 0" for line in checkout_steps[0]), (
        "release.yml build job's checkout does not declare `fetch-depth: 0`"
    )
    ancestry_step = _named_step(build_job, "Verify tagged commit is on main")
    assert "merge-base --is-ancestor" in _step_run_script(ancestry_step)


def test_release_workflow_publish_job_is_environment_gated():
    # Mirrors DECISIONS.md #8's blast-radius split: the OIDC token is minted only through
    # the `pypi` environment, and that job holds nothing else (no contents: write).
    publish_job = _job_block(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"), "publish")
    stripped = [line.strip() for line in publish_job]
    assert "environment: pypi" in stripped
    permissions_start = stripped.index("permissions:")
    granted = []
    for line in stripped[permissions_start + 1 :]:
        if not line or line.startswith("#"):
            continue
        if not re.match(r"^[a-z-]+: (read|write)$", line):
            break
        granted.append(line)
    assert granted == ["id-token: write"], f"publish job permissions are {granted!r}"


def test_release_ancestry_gate_refuses_an_annotated_tag_off_main(tmp_path):
    # Behavioural check of the step's own script: a fixture origin whose annotated v9.9.9
    # tag points at a commit that was never merged to main must be refused, and an
    # annotated tag at a main commit must pass. GITHUB_SHA is set to the *tag object* id,
    # as the runner may do for annotated tags, so the script must dereference HEAD itself.
    env = _hermetic_git_env(tmp_path)
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    runner = tmp_path / "runner"
    _git("init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path, env=env)
    _git("init", "--initial-branch=main", str(seed), cwd=tmp_path, env=env)
    (seed / "reviewed.txt").write_text("merged through a PR\n", encoding="utf-8")
    _git("add", ".", cwd=seed, env=env)
    _git("commit", "-q", "-m", "reviewed", cwd=seed, env=env)
    _git("tag", "-a", "v1.0.0", "-m", "release from main", cwd=seed, env=env)
    _git("checkout", "-q", "-b", "unreviewed", cwd=seed, env=env)
    (seed / "payload.txt").write_text("never reviewed\n", encoding="utf-8")
    _git("add", ".", cwd=seed, env=env)
    _git("commit", "-q", "-m", "unreviewed", cwd=seed, env=env)
    _git("tag", "-a", "v9.9.9", "-m", "release off main", cwd=seed, env=env)
    _git("remote", "add", "origin", str(origin), cwd=seed, env=env)
    _git("push", "-q", "origin", "main", "v1.0.0", "v9.9.9", cwd=seed, env=env)
    _git("clone", "-q", str(origin), str(runner), cwd=tmp_path, env=env)

    build_job = _job_block(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"), "build")
    script = _step_run_script(_named_step(build_job, "Verify tagged commit is on main"))

    _git("checkout", "-q", "--detach", "v9.9.9", cwd=runner, env=env)
    tag_object = _git("rev-parse", "v9.9.9", cwd=runner, env=env)
    refused = _run_step_script(
        script,
        cwd=runner,
        env={**env, "GITHUB_REF_NAME": "v9.9.9", "GITHUB_SHA": tag_object},
        scratch=tmp_path,
    )
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "not an ancestor of origin/main" in refused.stdout

    _git("checkout", "-q", "--detach", "v1.0.0", cwd=runner, env=env)
    tag_object = _git("rev-parse", "v1.0.0", cwd=runner, env=env)
    admitted = _run_step_script(
        script,
        cwd=runner,
        env={**env, "GITHUB_REF_NAME": "v1.0.0", "GITHUB_SHA": tag_object},
        scratch=tmp_path,
    )
    assert admitted.returncode == 0, admitted.stdout + admitted.stderr
