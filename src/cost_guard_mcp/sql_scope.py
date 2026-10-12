"""Fail-closed single-statement scanner for the SQL text the tools price and execute.

run_query_bounded prices the submitted text once (one BigQuery dry run / one EXPLAIN) and
then hands the identical text to the engine. Nothing in that path pins how many statements
the text contains, so a second statement riding behind a top-level ``;`` would execute
under a cap that was only ever checked against whatever subset the provider's estimate
chose to price. ``assert_single_statement`` closes that gap at the tool layer, before any
estimate or execute call, for every engine.

Design rule: every mistake must land in the refuse direction. The scanner therefore models
only the literal/comment forms it is sure a dialect has (and only for that dialect), and
refuses outright whenever two plausible readings of the text could disagree about where a
literal or comment ends - an identifier glued to a quote (a prefix in some dialects), a
backslash right before the closing delimiter of a raw/hex literal or quoted identifier, a
``/*`` inside a block comment, or any unterminated construct. No SQL parser dependency: the
text is model-controlled input and a parser's error path is itself an attack surface.
"""

import logging
import os
import re
from dataclasses import dataclass

from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.types import Engine

logger = logging.getLogger(__name__)

# Operator opt-out, read at call time. Set to exactly "1" to let multi-statement text
# through (the operator accepts that caps then price only what the provider's dry run /
# EXPLAIN happened to report for the whole script).
ALLOW_MULTI_STATEMENT_SQL_ENV_VAR = "COST_GUARD_ALLOW_MULTI_STATEMENT_SQL"

_CONTRACT = "estimate_query_cost/run_query_bounded accept exactly one SQL statement"

# Every character str.splitlines() treats as a line boundary - a superset of each dialect's
# line-comment terminators (Spark's lexer ends a '--' comment at a bare '\r', ZetaSQL's at
# any of [\r\n]). Ending a comment earlier than an engine does only exposes more text to the
# scanner (refuse direction); ending it later would hide a second statement behind it.
_LINE_TERMINATOR_RE = re.compile("[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


@dataclass(frozen=True)
class _Dialect:
    # How "..." is read: a cooked string with backslash escapes (BigQuery, Databricks) or a
    # quoted identifier where only "" doubling is special (Snowflake).
    double_quote_is_escaped_string: bool
    backtick_identifiers: bool
    hash_line_comments: bool
    double_slash_line_comments: bool
    dollar_strings: bool
    triple_quoted_strings: bool
    # Lower-cased identifier prefixes glued to a quote, by the quoting rules they select.
    raw_prefixes: frozenset[str]  # no escape processing: closes at the next delimiter
    cooked_prefixes: frozenset[str]  # backslash escapes, like an unprefixed string
    hex_prefixes: frozenset[str]  # X'..' - no escapes, single quotes only


_BIGQUERY = _Dialect(
    double_quote_is_escaped_string=True,
    backtick_identifiers=True,
    hash_line_comments=True,
    double_slash_line_comments=False,
    dollar_strings=False,
    triple_quoted_strings=True,
    raw_prefixes=frozenset({"r", "rb", "br"}),
    cooked_prefixes=frozenset({"b"}),
    hex_prefixes=frozenset(),
)
_SNOWFLAKE = _Dialect(
    double_quote_is_escaped_string=False,
    backtick_identifiers=False,
    hash_line_comments=False,
    double_slash_line_comments=True,
    dollar_strings=True,
    triple_quoted_strings=False,
    raw_prefixes=frozenset(),
    cooked_prefixes=frozenset(),
    hex_prefixes=frozenset(),
)
_DATABRICKS = _Dialect(
    double_quote_is_escaped_string=True,
    backtick_identifiers=True,
    hash_line_comments=False,
    double_slash_line_comments=False,
    dollar_strings=False,
    triple_quoted_strings=False,
    raw_prefixes=frozenset({"r"}),
    cooked_prefixes=frozenset(),
    hex_prefixes=frozenset({"x"}),
)
# An engine this module does not know gets only the constructs all three share. Nothing
# executes for such an engine anyway (the tools' own dispatch rejects it right after), so
# the choice only has to be non-admitting, never accurate.
_FALLBACK = _Dialect(
    double_quote_is_escaped_string=False,
    backtick_identifiers=False,
    hash_line_comments=False,
    double_slash_line_comments=False,
    dollar_strings=False,
    triple_quoted_strings=False,
    raw_prefixes=frozenset(),
    cooked_prefixes=frozenset(),
    hex_prefixes=frozenset(),
)
_DIALECTS: dict[str, _Dialect] = {
    "bigquery": _BIGQUERY,
    "snowflake": _SNOWFLAKE,
    "databricks": _DATABRICKS,
}


def _refuse(detail: str, position: int) -> UserVisibleError:
    # Never echo the SQL itself: it is model-controlled input, and the refusal text goes
    # straight back to the model. A character offset is enough to locate the problem.
    return UserVisibleError(f"{_CONTRACT}; {detail} (at character {position}).")


def _is_identifier_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _prefix_before(sql: str, index: int) -> str:
    """The identifier characters immediately (no whitespace) before ``index``."""
    start = index
    while start > 0 and _is_identifier_char(sql[start - 1]):
        start -= 1
    return sql[start:index]


def _line_comment_end(sql: str, index: int) -> int:
    terminator = _LINE_TERMINATOR_RE.search(sql, index)
    return len(sql) if terminator is None else terminator.end()


def _block_comment_end(sql: str, index: int) -> int:
    close = sql.find("*/", index + 2)
    if close == -1:
        raise _refuse("unterminated block comment", index)
    if "/*" in sql[index + 2 : close]:
        # Spark nests bracketed comments, BigQuery and Snowflake do not; the two readings
        # can be made to disagree about where the comment ends, so refuse rather than pick.
        raise _refuse(
            "a '/*' inside a block comment makes its end ambiguous (nested block comments "
            "are dialect-specific) - remove the inner '/*'",
            index,
        )
    return close + 2


def _escaped_literal_end(sql: str, body_start: int, delimiter: str, construct: str) -> int:
    """End (exclusive) of a literal whose body honours backslash escapes and, for a
    one-character delimiter, doubling of that delimiter."""
    index = body_start
    while index < len(sql):
        if sql[index] == "\\":
            index += 2
            continue
        if sql.startswith(delimiter, index):
            if len(delimiter) == 1 and sql.startswith(delimiter * 2, index):
                index += 2
                continue
            return index + len(delimiter)
        index += 1
    raise _refuse(f"unterminated {construct}", body_start - len(delimiter))


def _unescaped_literal_end(sql: str, body_start: int, delimiter: str, construct: str) -> int:
    """End (exclusive) of a literal whose body has NO escape processing (raw strings, hex
    literals, quoted identifiers), with doubling of a one-character delimiter.

    Some dialects still let a backslash protect the closing quote inside these forms; where
    a candidate closing delimiter sits behind an odd run of backslashes the escape-honouring
    and escape-free readings pick different closing quotes, so that shape is refused rather
    than guessed. Candidates are examined one at a time, and the backslash test comes BEFORE
    the doubled-delimiter test: a backslash-protected quote followed by a second quote is the
    ambiguous shape, not a doubled pair to skip."""
    opener = body_start - len(delimiter)
    close = sql.find(delimiter, body_start)
    while close != -1:
        backslash_run = 0
        while close - backslash_run - 1 >= body_start and sql[close - backslash_run - 1] == "\\":
            backslash_run += 1
        if backslash_run % 2 == 1:
            raise _refuse(
                f"a backslash immediately before the closing delimiter of a {construct} makes "
                "its end ambiguous across dialects - escape it differently or drop it",
                opener,
            )
        if len(delimiter) == 1 and sql.startswith(delimiter * 2, close):
            close = sql.find(delimiter, close + 2)
            continue
        return close + len(delimiter)
    raise _refuse(f"unterminated {construct}", opener)


def _quoted_end(sql: str, index: int, dialect: _Dialect) -> int:
    """End (exclusive) of the ' or \" delimited construct opening at ``index``."""
    quote = sql[index]
    prefix = _prefix_before(sql, index).lower()
    if dialect.triple_quoted_strings and sql.startswith(quote * 3, index):
        delimiter = quote * 3
    else:
        delimiter = quote
    body_start = index + len(delimiter)

    if prefix == "":
        if quote == '"' and not dialect.double_quote_is_escaped_string:
            return _unescaped_literal_end(sql, body_start, delimiter, "quoted identifier")
        return _escaped_literal_end(sql, body_start, delimiter, "string literal")
    if prefix in dialect.raw_prefixes:
        return _unescaped_literal_end(sql, body_start, delimiter, "raw string literal")
    if prefix in dialect.cooked_prefixes:
        return _escaped_literal_end(sql, body_start, delimiter, "bytes literal")
    if prefix in dialect.hex_prefixes and delimiter == "'":
        return _unescaped_literal_end(sql, body_start, delimiter, "hex literal")
    raise _refuse(
        "an identifier directly attached to a quote is read as a literal prefix with "
        "different quoting rules by some dialects - put whitespace between them",
        index - len(prefix),
    )


def _scan(sql: str, dialect: _Dialect) -> None:
    index = 0
    terminator_seen = False
    while index < len(sql):
        ch = sql[index]
        if (
            sql.startswith("--", index)
            or (dialect.hash_line_comments and ch == "#")
            or (dialect.double_slash_line_comments and sql.startswith("//", index))
        ):
            index = _line_comment_end(sql, index)
            continue
        if sql.startswith("/*", index):
            index = _block_comment_end(sql, index)
            continue
        if ch.isspace():
            index += 1
            continue
        if terminator_seen:
            raise _refuse(
                "found ';' followed by more SQL. Submit statements one call at a time so each "
                "is cost-estimated and capped on its own (operators who knowingly run "
                f"multi-statement scripts can set {ALLOW_MULTI_STATEMENT_SQL_ENV_VAR}=1 on "
                "the server)",
                index,
            )
        if ch == ";":
            terminator_seen = True
            index += 1
            continue
        if ch in "'\"":
            index = _quoted_end(sql, index, dialect)
            continue
        if ch == "`" and dialect.backtick_identifiers:
            index = _unescaped_literal_end(sql, index + 1, "`", "quoted identifier")
            continue
        if dialect.dollar_strings and sql.startswith("$$", index):
            if _prefix_before(sql, index):
                raise _refuse(
                    "'$$' directly attached to an identifier is ambiguous ($ is an identifier "
                    "character) - put whitespace before it",
                    index,
                )
            index = _unescaped_literal_end(sql, index + 2, "$$", "dollar-quoted string")
            continue
        index += 1


def assert_single_statement(sql: str, engine: Engine) -> None:
    """Raise UserVisibleError unless ``sql`` is exactly one statement for ``engine``.

    A single trailing ``;`` (followed only by whitespace and comments) is allowed - the
    engines' LIMIT wrappers already strip it. Everything the scanner cannot resolve with
    certainty is refused; see the module docstring for the rule.
    """
    if os.environ.get(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR) == "1":
        logger.info(
            "engine=%s single_statement_check=skipped reason=%s=1",
            engine,
            ALLOW_MULTI_STATEMENT_SQL_ENV_VAR,
        )
        return
    try:
        _scan(sql, _DIALECTS.get(engine, _FALLBACK))
    except UserVisibleError as exc:
        logger.info("engine=%s single_statement_check=refused detail=%s", engine, exc)
        raise
