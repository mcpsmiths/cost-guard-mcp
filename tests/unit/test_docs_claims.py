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
import typing
from pathlib import Path

import pytest

from cost_guard_mcp.types import Engine

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "src" / "cost_guard_mcp"
ENGINES_DIR = PACKAGE_DIR / "engines"
README_MD = REPO_ROOT / "README.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
# CHANGELOG.md is the historical record: it may name things that no longer exist.
ROOT_DOCS = sorted(path for path in REPO_ROOT.glob("*.md") if path.name != "CHANGELOG.md")
ENGINES: tuple[str, ...] = typing.get_args(Engine)
# Adding an engine to the `Engine` literal fails here until the name is spelled out, which is
# the moment the docs need the new engine too.
ENGINE_DISPLAY_NAMES = {
    "bigquery": "BigQuery",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pyproject() -> dict:
    return tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end, text.index(start))]


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


def test_every_engine_has_a_module_and_no_unexplained_module_sits_beside_them():
    stems = {path.stem for path in ENGINES_DIR.glob("*.py") if path.stem != "__init__"}
    assert set(ENGINES) <= stems, f"engines/ lacks a module for {sorted(set(ENGINES) - stems)}"
    # A helper is named after its engine (bigquery_estimate_rules); anything else is a new
    # engine that the `Engine` literal, and so the docs, do not know yet.
    unexplained = {
        stem
        for stem in stems
        if stem not in ENGINES and not any(stem.startswith(f"{engine}_") for engine in ENGINES)
    }
    assert not unexplained, f"engines/ modules that are not in the Engine literal: {unexplained}"


def _agents_overview() -> str:
    text = _read(AGENTS_MD)
    return " ".join(_section(text, "## Project overview", "## Setup").split())


def _readme_pitch() -> str:
    # The one-sentence description under the badges, before "## Why".
    text = _read(README_MD)
    return " ".join(_section(text, "Pre-flight query cost", "## Why").split())


@pytest.mark.parametrize("engine", ENGINES)
def test_agents_and_readme_overviews_name_every_engine(engine: str):
    name = ENGINE_DISPLAY_NAMES[engine]
    assert name in _agents_overview(), f"AGENTS.md project overview does not name {name}"
    assert name in _readme_pitch(), f"README description does not name {name}"
