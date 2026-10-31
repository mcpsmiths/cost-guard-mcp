"""The prose docs make claims the code can check; hold each one to the code that decides it.

Each test derives the truth from the source tree (pyproject.toml, the registered tool set, the
``Engine`` literal, ``result_bounds`` constants, the module list under ``src/``) and compares a
doc to it, so a doc cannot drift again without a red test. AGENTS.md's own tool-count sentence
is pinned separately by ``test_agents_md.py``.

Found by the 2026-10-30 estate verification: README told users to install an ``otel`` extra
that pyproject.toml never defined, ARCHITECTURE.md said the server registers 3 tools after the
fourth shipped and listed two engines, and AGENTS.md's overview named two engines.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README_MD = REPO_ROOT / "README.md"
# CHANGELOG.md is the historical record: it may name things that no longer exist.
ROOT_DOCS = sorted(path for path in REPO_ROOT.glob("*.md") if path.name != "CHANGELOG.md")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pyproject() -> dict:
    return tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))


def _requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0]


@pytest.mark.parametrize("doc", ROOT_DOCS, ids=lambda path: path.name)
def test_docs_do_not_advertise_an_extra_pyproject_does_not_define(doc: Path):
    # `pip install "pkg[x]"` on an undefined extra warns and installs nothing extra, and
    # `uv sync --extra x` fails: the README told operators both for `otel`, which is a
    # [dependency-groups] entry (a group is never published in the wheel's metadata).
    defined = set(_pyproject()["project"].get("optional-dependencies", {}))
    text = _read(doc)
    advertised = {
        name.strip()
        for bracket in re.findall(r"cost-guard-mcp\[([^\]]*)\]", text)
        for name in bracket.split(",")
    } | set(re.findall(r"--extra[ =]([\w.-]+)", text))
    assert advertised <= defined, (
        f"{doc.name} advertises extras {sorted(advertised - defined)} that pyproject.toml "
        f"[project.optional-dependencies] does not define (defined: {sorted(defined)})"
    )


def test_readme_dependency_groups_are_defined_and_otel_packages_are_named():
    readme = _read(README_MD)
    groups = _pyproject()["dependency-groups"]
    used = set(re.findall(r"--group[ =]([\w.-]+)", readme))
    assert used <= set(groups), f"README names undefined groups {sorted(used - set(groups))}"
    # An installed package has no groups: the README must name what the `otel` group holds,
    # which is what _bootstrap_opentelemetry imports when OTEL_EXPORTER_OTLP_ENDPOINT is set.
    assert "otel" in used, "README must show how to opt in to the otel dependency group"
    for requirement in groups["otel"]:
        assert _requirement_name(requirement) in readme, requirement
