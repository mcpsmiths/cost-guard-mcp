"""Pins for the `snowflake-connector-python` cap, its security floor and its Dependabot ignore.

`snowflake-connector-python` is capped below 5 because 5.0.0 is a public preview published
under a plain version number (DECISIONS.md #23, issue #121). Before this file nothing asserted
the snowflake specifier, the snowflake Dependabot ignore, or the locked snowflake version:
removing the cap, widening it, or dropping the Dependabot ignore that keeps the grouped
`python-dependencies` PR green all left the suite green, and a fresh resolve would have picked
the preview driver again.

Lifting the snowflake cap is a deliberate change (the checklist lives on #121): update these
assertions in the same PR, together with DECISIONS.md #23.

tomllib parses pyproject.toml and uv.lock; dependabot.yml is read as text because the project
has no YAML dependency.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
UV_LOCK_PATH = REPO_ROOT / "uv.lock"
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"

SNOWFLAKE = "snowflake-connector-python"

# DECISIONS.md #23: the cap stays until Snowflake ships a GA 5.x and the lift checklist is done.
SNOWFLAKE_UPPER_BOUND_EXCLUSIVE = (5, 0, 0)
# The floor is the CVE-2026-15925 / GHSA-5cc2-282f-jjq2 fix (TLS hostname-verification bypass,
# affects >=4.0.0,<4.7.1); see docs/superpowers/plans/2026-09-18-next-upgrade-plan.md.
SNOWFLAKE_MIN_LOWER_BOUND = (4, 7, 1)

_CLAUSE_RE = re.compile(r"(<=|>=|==|!=|~=|<|>)\s*([0-9][0-9A-Za-z.]*)")
_REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version(text: str) -> tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r"\d+", text.split("+")[0])][:3]
    return tuple(numbers + [0] * (3 - len(numbers)))


def _clauses(specifier: str) -> list[tuple[str, tuple[int, ...]]]:
    return [(op, _version(ver)) for op, ver in _CLAUSE_RE.findall(specifier)]


def _satisfies(version: tuple[int, ...], specifier: str) -> bool:
    clauses = _clauses(specifier)
    assert clauses or not specifier.strip(), f"unparsed specifier {specifier!r}"
    checks = {
        ">=": lambda bound: version >= bound,
        ">": lambda bound: version > bound,
        "<=": lambda bound: version <= bound,
        "<": lambda bound: version < bound,
        "==": lambda bound: version == bound,
        "!=": lambda bound: version != bound,
    }
    for op, bound in clauses:
        assert op in checks, f"specifier operator {op!r} in {specifier!r} is not supported here"
        if not checks[op](bound):
            return False
    return True


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _declared_requirements(name: str) -> list[str]:
    """Every specifier declared for `name` that can constrain the resolve.

    Covers [project].dependencies, [project.optional-dependencies], [dependency-groups] and
    the [tool.uv] constraint / override lists, so a cap added in any of them is seen.
    """
    pyproject = _pyproject()
    project = pyproject.get("project", {})
    uv_table = pyproject.get("tool", {}).get("uv", {})
    pools: list[list] = [project.get("dependencies", [])]
    pools.extend(project.get("optional-dependencies", {}).values())
    pools.extend(pyproject.get("dependency-groups", {}).values())
    pools.append(uv_table.get("constraint-dependencies", []))
    pools.append(uv_table.get("override-dependencies", []))
    found = []
    for pool in pools:
        for entry in pool:
            if not isinstance(entry, str):
                continue
            match = _REQUIREMENT_RE.match(entry)
            if match and _normalize(match.group(1)) == name:
                found.append(match.group(2).strip())
    return found


def _declared_specifier(name: str) -> str:
    specifiers = _declared_requirements(name)
    assert len(specifiers) == 1, f"expected one declared requirement for {name}: {specifiers!r}"
    return specifiers[0]


def _locked_versions() -> dict[str, str]:
    lock = tomllib.loads(UV_LOCK_PATH.read_text(encoding="utf-8"))
    return {_normalize(pkg["name"]): pkg["version"] for pkg in lock["package"]}


def _locked_version(name: str) -> tuple[int, ...]:
    locked = _locked_versions()
    assert name in locked, f"uv.lock has no [[package]] named {name}"
    return _version(locked[name])


def _dependabot_uv_block() -> str:
    """The `package-ecosystem: "uv"` entry of dependabot.yml with comment lines removed."""
    lines = [
        line
        for line in DEPENDABOT_PATH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    blocks: list[list[str]] = []
    for line in lines:
        if re.match(r"\s*-\s*package-ecosystem:", line):
            blocks.append([])
        if blocks:
            blocks[-1].append(line)
    uv_blocks = [
        "\n".join(block)
        for block in blocks
        if re.search(r"package-ecosystem:\s*[\"']?uv[\"']?\s*$", block[0])
    ]
    assert len(uv_blocks) == 1, f"expected one uv ecosystem in dependabot.yml, got {len(uv_blocks)}"
    return uv_blocks[0]


# ---- snowflake-connector-python <5 (DECISIONS.md #23, issue #121) ----


def test_snowflake_connector_requirement_keeps_an_upper_bound_below_5():
    clauses = _clauses(_declared_specifier(SNOWFLAKE))
    upper_bounds = [
        bound
        for op, bound in clauses
        if (op == "<" and bound <= SNOWFLAKE_UPPER_BOUND_EXCLUSIVE)
        or (op == "<=" and bound < SNOWFLAKE_UPPER_BOUND_EXCLUSIVE)
    ]
    assert upper_bounds, (
        f"{SNOWFLAKE} must stay capped below 5 (5.0.0 is a public preview, DECISIONS.md #23, "
        f"issue #121); declared specifier: {_declared_specifier(SNOWFLAKE)!r}"
    )


def test_snowflake_connector_requirement_keeps_a_lower_bound_of_at_least_4_7_1():
    lower_bounds = [bound for op, bound in _clauses(_declared_specifier(SNOWFLAKE)) if op == ">="]
    assert lower_bounds, f"{SNOWFLAKE} lost its lower bound"
    assert max(lower_bounds) >= SNOWFLAKE_MIN_LOWER_BOUND, (
        f"{SNOWFLAKE} floor {max(lower_bounds)} is below 4.7.1, the first release without the "
        "TLS hostname-verification bypass (CVE-2026-15925 / GHSA-5cc2-282f-jjq2)"
    )


def test_snowflake_connector_is_declared_exactly_once():
    # A second requirement line (a group, a constraint or an override) could widen the range
    # the resolver sees without touching the line the two tests above read.
    assert len(_declared_requirements(SNOWFLAKE)) == 1, _declared_requirements(SNOWFLAKE)


def test_locked_snowflake_connector_satisfies_the_declared_range():
    locked = _locked_version(SNOWFLAKE)
    specifier = _declared_specifier(SNOWFLAKE)
    assert _satisfies(locked, specifier), f"uv.lock pins {SNOWFLAKE} {locked}, outside {specifier}"
    assert locked < SNOWFLAKE_UPPER_BOUND_EXCLUSIVE, (
        f"uv.lock resolves {SNOWFLAKE} {locked}: the 5.x preview driver is locked"
    )


def test_dependabot_ignores_snowflake_connector_major_updates():
    # Without the ignore the grouped python-dependencies PR proposes 5.x, which fails mypy and
    # the connector contract tests by design (#121), and a merge of that PR would fight the cap.
    block = _dependabot_uv_block()
    match = re.search(
        r"dependency-name:\s*[\"']?" + re.escape(SNOWFLAKE) + r"[\"']?\s*\n\s*"
        r"update-types:\s*\[([^\]]*)\]",
        block,
    )
    assert match, f"dependabot.yml uv ecosystem no longer ignores {SNOWFLAKE} (issue #121)"
    assert "version-update:semver-major" in match.group(1), (
        f"the {SNOWFLAKE} ignore no longer covers semver-major updates: {match.group(1)!r}"
    )
