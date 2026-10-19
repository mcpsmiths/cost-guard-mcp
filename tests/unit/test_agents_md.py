"""AGENTS.md's project overview names every registered tool (#111, item 2).

The overview sentence said "three tools (...)" after the fourth, check_credentials, had
shipped. This holds the sentence and server.py in lockstep: the count word and the backticked
names must match the `@mcp.tool(` registrations, and those must match what tools/list serves.
"""

import asyncio
import re
from pathlib import Path

from cost_guard_mcp.server import mcp

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PY = REPO_ROOT / "src" / "cost_guard_mcp" / "server.py"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _decorated_tool_names() -> list[str]:
    # The mcprush static reader's contract, and this project's registration shape: a
    # single-line `@mcp.tool(` directly above the `def`/`async def` it registers.
    lines = SERVER_PY.read_text(encoding="utf-8").splitlines()
    names = []
    for index, line in enumerate(lines):
        if line.startswith("@mcp.tool("):
            match = re.match(r"(?:async )?def (\w+)\(", lines[index + 1])
            assert match, f"server.py:{index + 1}: @mcp.tool( is not directly above a def"
            names.append(match.group(1))
    return names


def _overview_tools_phrase() -> re.Match[str]:
    text = AGENTS_MD.read_text(encoding="utf-8")
    section = text[text.index("## Project overview") : text.index("## Setup")]
    unwrapped = " ".join(section.split())
    match = re.search(r"\b(\w+) tools \(([^)]*)\)", unwrapped)
    assert match, "AGENTS.md project overview has no '<count> tools (`a`, `b`, ...)' phrase"
    return match


def test_agents_md_overview_counts_and_names_every_registered_tool():
    decorated = _decorated_tool_names()
    registered = sorted(tool.name for tool in asyncio.run(mcp.list_tools()))
    assert sorted(decorated) == registered  # the static count is the served tool set

    phrase = _overview_tools_phrase()
    count_word = phrase.group(1)
    counted = _NUMBER_WORDS.get(count_word)
    assert counted is not None, (
        f"AGENTS.md says {count_word!r} tools; spell the count as a word, one of "
        f"{', '.join(_NUMBER_WORDS)} (in: {phrase.group(0)})"
    )
    assert counted == len(decorated), phrase.group(0)
    assert sorted(re.findall(r"`(\w+)`", phrase.group(2))) == registered, phrase.group(0)
