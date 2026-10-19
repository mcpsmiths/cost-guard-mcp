"""Read-statement allowlist for the SQL text the tools price and execute (DECISIONS.md #13).

Warehouse rows re-enter the model's context, and a model that has just read a row can be
steered by it into any statement the deployed credential allows. By default both tools
therefore accept only statements whose first keyword - after whitespace, comments and any
opening parentheses - is one of ``READ_STATEMENT_KEYWORDS``, and a ``WITH`` only when the
walk over its CTE list finds ``SELECT`` or ``VALUES`` as the main statement: Spark SQL's
grammar is ``ctes? dmlStatementNoWith``, so ``WITH ... INSERT`` is one statement whose first
keyword is WITH and whose effect is a write.

The gate is defence in depth, not the authorisation boundary. The boundary is the deployed
credential's grants (a read-only principal is recommended; SECURITY.md), and a SELECT can
still call a remote function, UDF or system function with side effects. The lexer is shared
with ``sql_scope`` so the two gates never disagree about where a comment or literal ends, and
every shape the walker cannot resolve is refused - a mistake must land in the refuse direction.
The walk is bounded too (``_MAX_CTE_NESTING``): the text is model-controlled, and a gate that
can be made slow or made to overflow the stack is a gate that can be made to fail generically.
"""

import logging
import os
import re
from dataclasses import dataclass

from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.sql_scope import (
    LEXER_OPENERS,
    _Dialect,
    comment_end,
    dialect_for,
    is_single_statement,
    literal_end,
)
from cost_guard_mcp.types import Engine

logger = logging.getLogger(__name__)

# Operator opt-out, read at call time; exactly "1", like the multi-statement opt-out.
ALLOW_WRITE_STATEMENTS_ENV_VAR = "COST_GUARD_ALLOW_WRITE_STATEMENTS"

READ_STATEMENT_KEYWORDS = frozenset(
    {"SELECT", "WITH", "VALUES", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
)
# What may follow a CTE list or open a CTE body - narrower than the allowlist on purpose:
# SHOW/DESCRIBE/EXPLAIN cannot follow a WITH in any of the three grammars.
_QUERY_KEYWORDS = frozenset({"SELECT", "VALUES"})

_CONTRACT = (
    "estimate_query_cost/run_query_bounded accept only read statements (first keyword "
    "SELECT, WITH, VALUES, SHOW, DESCRIBE, DESC or EXPLAIN)"
)
_HINT = (
    "Warehouse rows re-enter the model's context, so writes are refused by default whatever "
    "the credential allows; an operator who intends this server to run writes under its "
    f"credential can set {ALLOW_WRITE_STATEMENTS_ENV_VAR}=1 on the server (and should first "
    "confirm that credential's grants)."
)

# ASCII only, deliberately: str.upper() maps U+017F to "S", so a Unicode-aware token would let
# "ſelect" pass as SELECT; no engine lexes a non-ASCII token as a keyword, so refusing one
# costs nothing. "$" is not part of a keyword (SELECT$x yields SELECT, then junk).
_KEYWORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
# CTE names use sql_scope's identifier alphabet: Snowflake allows "$" in an identifier (x$1).
_CTE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*", re.ASCII)

_WITH = "WITH"
_RECURSIVE = "RECURSIVE"

# How deep a WITH may sit inside another (a CTE body, or the parenthesised main statement the
# walk descends into). The walk re-scans the enclosed text once per level, so unbounded it was
# quadratic in one model-controlled string (496 levels padded to 500 KB: 47 s, holding a
# run_query_bounded in-flight slot) and past ~497 levels overran the interpreter's recursion
# limit, where the RecursionError surfaced as the generic tool error instead of this gate's
# labelled refusal. Real queries nest two or three; 16 caps the worst case at 16 scans.
_MAX_CTE_NESTING = 16


@dataclass(frozen=True)
class _Frame:
    """Where the walk is: the statement it is checking (for the refusal text), the offset of
    the text it holds within the submitted SQL (CTE bodies are walked as slices), and how
    many WITHs enclose that text."""

    which: str
    base: int
    depth: int

    def inner(self, shift: int) -> "_Frame":
        """The frame for a slice starting ``shift`` characters into this one, one WITH deeper."""
        return _Frame(self.which, self.base + shift, self.depth + 1)


def _refuse(detail: str, position: int) -> UserVisibleError:
    # Never the SQL itself: model-controlled input whose refusal goes straight back to the
    # model. A character offset locates the problem; the hint names the opt-out.
    return UserVisibleError(f"{_CONTRACT}; {detail} (at character {position}). {_HINT}")


def _lexeme_end(sql: str, index: int, dialect: _Dialect) -> int | None:
    """End of the comment or literal opening at ``index``, None when neither does. One set
    membership test settles the common character before the lexer is consulted."""
    if sql[index] not in LEXER_OPENERS:
        return None
    end = comment_end(sql, index, dialect)
    return literal_end(sql, index, dialect) if end is None else end


def _skip_trivia(sql: str, index: int, dialect: _Dialect, *, parens: bool) -> int:
    """Advance past whitespace, comments and - when ``parens`` - opening parentheses."""
    while index < len(sql):
        ch = sql[index]
        if ch.isspace() or (parens and ch == "("):
            index += 1
            continue
        comment = comment_end(sql, index, dialect) if ch in LEXER_OPENERS else None
        if comment is None:
            break
        index = comment
    return index


def _word_at(sql: str, index: int) -> str | None:
    match = _KEYWORD_RE.match(sql, index)
    return None if match is None else match.group().upper()


def first_keyword(sql: str, dialect: _Dialect) -> tuple[str | None, int]:
    """The upper-cased first keyword of ``sql`` - the first ASCII word after whitespace,
    comments and any number of opening parentheses - and its offset; ``(None, offset)``
    when no word starts there (a literal, a digit, a BOM, ``;``, end of text)."""
    index = _skip_trivia(sql, 0, dialect, parens=True)
    return _word_at(sql, index), index


def _group_end(sql: str, index: int, dialect: _Dialect) -> int:
    """End (exclusive) of the parenthesised group opening at ``index``, literal- and
    comment-aware; refuses an unbalanced group."""
    depth = 0
    position = index
    while position < len(sql):
        skipped = _lexeme_end(sql, position, dialect)
        if skipped is not None:
            position = skipped
            continue
        if sql[position] == "(":
            depth += 1
        elif sql[position] == ")":
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    raise _refuse("a parenthesised group is not closed", index)


def _cte_name_end(sql: str, index: int, dialect: _Dialect) -> int | None:
    match = _CTE_NAME_RE.match(sql, index)
    if match is not None:
        return match.end()
    if index >= len(sql):
        return None
    is_quoted_identifier = (sql[index] == '"' and not dialect.double_quote_is_escaped_string) or (
        sql[index] == "`" and dialect.backtick_identifiers
    )
    return literal_end(sql, index, dialect) if is_quoted_identifier else None


def _resolve_query_keyword(sql: str, dialect: _Dialect, frame: _Frame) -> tuple[str | None, int]:
    """``first_keyword`` with a WITH resolved through its CTE list to the main statement;
    a WITH nested past ``_MAX_CTE_NESTING`` is refused rather than walked."""
    keyword, offset = first_keyword(sql, dialect)
    if keyword != _WITH:
        return keyword, offset
    if frame.depth > _MAX_CTE_NESTING:
        raise _refuse(
            f"the WITH clause of {frame.which} nests CTEs more than {_MAX_CTE_NESTING} levels "
            "deep - rewrite without the nested CTEs",
            frame.base + offset,
        )
    return _main_keyword_after_ctes(sql, offset + len(_WITH), dialect, frame)


def _main_keyword_after_ctes(
    sql: str, index: int, dialect: _Dialect, frame: _Frame
) -> tuple[str | None, int]:
    """Walk ``name [(columns)] AS (body)``, comma separated, from just after a WITH, and
    return the main statement's keyword (resolved through parentheses and nested WITHs)
    with its offset; ``(None, offset)`` at the first shape the walk cannot resolve. Every
    CTE body must itself resolve to SELECT/VALUES (data-modifying CTEs exist elsewhere)."""
    index = _skip_trivia(sql, index, dialect, parens=False)
    if _word_at(sql, index) == _RECURSIVE:
        index = _skip_trivia(sql, index + len(_RECURSIVE), dialect, parens=False)
    while True:
        name_end = _cte_name_end(sql, index, dialect)
        if name_end is None:
            return None, index
        index = _skip_trivia(sql, name_end, dialect, parens=False)
        if sql.startswith("(", index):  # optional column list
            index = _skip_trivia(sql, _group_end(sql, index, dialect), dialect, parens=False)
        if _word_at(sql, index) != "AS":
            return None, index
        index = _skip_trivia(sql, index + len("AS"), dialect, parens=False)
        if not sql.startswith("(", index):
            return None, index
        body_end = _group_end(sql, index, dialect)
        body = sql[index + 1 : body_end - 1]
        body_keyword, body_offset = _resolve_query_keyword(body, dialect, frame.inner(index + 1))
        if body_keyword not in _QUERY_KEYWORDS:
            return None, index + 1 + body_offset
        index = _skip_trivia(sql, body_end, dialect, parens=False)
        if sql.startswith(",", index):
            index = _skip_trivia(sql, index + 1, dialect, parens=False)
            continue
        if sql.startswith("(", index):
            keyword, offset = _resolve_query_keyword(sql[index:], dialect, frame.inner(index))
            return keyword, index + offset
        return _word_at(sql, index), index


def _statement_segments(sql: str, dialect: _Dialect) -> list[tuple[int, int]]:
    """``(start, end)`` of every top-level ``;``-separated segment, comments and literals
    skipped with the shared lexer (which raises its own refusals for unresolvable text)."""
    segments: list[tuple[int, int]] = []
    start = index = 0
    while index < len(sql):
        skipped = _lexeme_end(sql, index, dialect)
        if skipped is not None:
            index = skipped
            continue
        if sql[index] == ";":
            segments.append((start, index))
            start = index = index + 1
            continue
        index += 1
    segments.append((start, len(sql)))
    return segments


def _check_statement(segment: str, base: int, ordinal: int | None, dialect: _Dialect) -> None:
    which = "the statement" if ordinal is None else f"statement {ordinal}"
    keyword, offset = first_keyword(segment, dialect)
    if keyword == _WITH:
        frame = _Frame(which, base, depth=1)
        main, main_offset = _main_keyword_after_ctes(segment, offset + len(_WITH), dialect, frame)
        if main not in _QUERY_KEYWORDS:
            raise _refuse(
                f"the WITH clause of {which} is not followed by a SELECT or VALUES the gate "
                "could identify - rewrite without the CTE",
                base + main_offset,
            )
        return
    if keyword is None:
        raise _refuse(f"{which} does not start with a keyword", base + offset)
    if keyword not in READ_STATEMENT_KEYWORDS:
        raise _refuse(f"{which} starts with a different keyword", base + offset)


def _check_every_statement(sql: str, dialect: _Dialect) -> None:
    # By default (single-statement gate active) this is exactly one segment; under the
    # multi-statement opt-out every statement must pass - the gate cannot vouch for text it
    # did not look at. Blank segments (whitespace/comments only) hold no statement.
    segments = [
        (start, end)
        for start, end in _statement_segments(sql, dialect)
        if _skip_trivia(sql[start:end], 0, dialect, parens=False) < end - start
    ]
    if not segments:
        raise _refuse("found no SQL statement (empty, whitespace or comments only)", 0)
    for ordinal, (start, end) in enumerate(segments, 1):
        _check_statement(sql[start:end], start, ordinal if len(segments) > 1 else None, dialect)


def _is_recursive_with(sql: str, dialect: _Dialect) -> bool:
    """True when the statement's own WITH - the first keyword after whitespace, comments and
    opening parentheses - is ``WITH RECURSIVE``. Only that outermost WITH counts: a recursive
    CTE nested inside a CTE body is the warehouse's business, not the wrap's."""
    keyword, offset = first_keyword(sql, dialect)
    if keyword != _WITH:
        return False
    index = _skip_trivia(sql, offset + len(_WITH), dialect, parens=False)
    return _word_at(sql, index) == _RECURSIVE


def is_single_query_statement(sql: str, engine: Engine) -> bool:
    """May the engine enclose ``sql`` in ``SELECT * FROM (...) AS cost_guard_row_cap LIMIT n``
    (#94)? True iff it is one statement whose main keyword - resolved through any CTE list by
    the same walk the read gate uses - is SELECT or VALUES, and whose own WITH is not
    ``WITH RECURSIVE``. SHOW/DESCRIBE/EXPLAIN, scripts, writes and ``WITH ... INSERT`` (Spark
    allows it, and the write opt-out skips the gate) are not valid as a derived table; a
    recursive CTE is admitted by the read gate but BigQuery allows ``WITH RECURSIVE`` only at
    the top level of a statement (a compile-time 400 inside the wrap, #124), so it runs
    unwrapped and the paged fetch is its row cap, like every other admitted-but-unwrapped
    statement. Never raises and never logs: a shape the walk refuses (unbalanced parentheses,
    a WITH nested past ``_MAX_CTE_NESTING``) is simply False.
    """
    if not is_single_statement(sql, engine):
        return False
    dialect = dialect_for(engine)
    try:
        if _is_recursive_with(sql, dialect):
            return False
        keyword, _ = _resolve_query_keyword(sql, dialect, _Frame("the statement", 0, depth=1))
    except UserVisibleError:
        return False
    return keyword in _QUERY_KEYWORDS


def assert_read_statement(sql: str, engine: Engine) -> None:
    """Raise UserVisibleError unless every statement in ``sql`` is a read for ``engine``.

    Runs immediately after ``assert_single_statement`` in both tools, so it sees text already
    proven to be one statement (or knowingly multi-statement under that gate's opt-out).
    """
    if os.environ.get(ALLOW_WRITE_STATEMENTS_ENV_VAR) == "1":
        logger.info(
            "engine=%s read_statement_check=skipped reason=%s=1",
            engine,
            ALLOW_WRITE_STATEMENTS_ENV_VAR,
        )
        return
    try:
        _check_every_statement(sql, dialect_for(engine))
    except UserVisibleError as exc:
        logger.info("engine=%s read_statement_check=refused detail=%s", engine, exc)
        raise
