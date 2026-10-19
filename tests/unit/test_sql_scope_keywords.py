"""DECISIONS.md #13: the tools accept only read statements by default."""

import logging
import time

import pytest

from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.sql_scope import (
    ALLOW_MULTI_STATEMENT_SQL_ENV_VAR,
    LEXER_OPENERS,
    comment_end,
    dialect_for,
    literal_end,
)
from cost_guard_mcp.sql_scope_keywords import (
    _MAX_CTE_NESTING,
    ALLOW_WRITE_STATEMENTS_ENV_VAR,
    READ_STATEMENT_KEYWORDS,
    assert_read_statement,
    first_keyword,
    is_single_query_statement,
)

ENGINES = ["bigquery", "snowflake", "databricks"]
_READ_ONLY = "only read statements"
_LOGGER_NAME = "cost_guard_mcp.sql_scope_keywords"


def _refuses(sql: str, engine: str, match: str = _READ_ONLY) -> str:
    with pytest.raises(UserVisibleError, match=match) as excinfo:
        assert_read_statement(sql, engine)  # type: ignore[arg-type]
    return str(excinfo.value)


def _accepts(sql: str, engine: str) -> None:
    assert_read_statement(sql, engine)  # type: ignore[arg-type]


def test_allowlist_is_the_owner_confirmed_set():
    assert READ_STATEMENT_KEYWORDS == {
        "SELECT",
        "WITH",
        "VALUES",
        "SHOW",
        "DESCRIBE",
        "DESC",
        "EXPLAIN",
    }


# --- admitted -------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",
        "Select 1",
        "SELECT 1;",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "VALUES (1)",
        "SHOW TABLES",
        "DESCRIBE TABLE t",
        "DESC t",
        "EXPLAIN SELECT 1",
        # leading comments and parentheses
        "-- c\nSELECT 1",
        "/* c */ SELECT 1",
        "(SELECT 1)",
        "((SELECT 1))",
        "( -- c\n (SELECT 1))",
        # keyword glued to punctuation
        "SELECT(1)",
        "SELECT/*c*/1",
        "SELECT\n1",
        # WITH shapes
        "WITH RECURSIVE r AS (SELECT 1) SELECT * FROM r",
        "WITH a (x) AS (SELECT 1), b AS (SELECT ')' AS p -- )\n) SELECT * FROM a, b",
        "WITH x AS (SELECT 1) (SELECT * FROM x)",
        "WITH x AS (SELECT 1) VALUES (1)",
        "WITH x AS ((SELECT 1)) SELECT 1",
        "WITH x AS (WITH y AS (SELECT 1) SELECT * FROM y) SELECT * FROM x",
        "WITH x AS (SELECT 1) (WITH y AS (SELECT 2) SELECT * FROM y)",
        "with x as (select 1) select * from x",
    ],
)
def test_admits_read_statements_on_every_engine(sql, engine):
    _accepts(sql, engine)


def test_dialect_specific_comment_forms_are_skipped_only_where_the_dialect_has_them():
    _accepts("# c\nSELECT 1", "bigquery")
    _refuses("# c\nSELECT 1", "snowflake")
    _refuses("# c\nSELECT 1", "databricks")
    _accepts("// c\nSELECT 1", "snowflake")
    _refuses("// c\nSELECT 1", "bigquery")


def test_quoted_cte_names_use_each_dialects_identifier_quotes():
    _accepts('WITH "x" AS (SELECT 1) SELECT * FROM "x"', "snowflake")
    _accepts("WITH `x` AS (SELECT 1) SELECT * FROM `x`", "bigquery")
    _accepts("WITH `x` AS (SELECT 1) SELECT * FROM `x`", "databricks")
    # "..." is a string literal, not an identifier, on BigQuery and Databricks.
    _refuses('WITH "x" AS (SELECT 1) SELECT * FROM "x"', "bigquery")
    _refuses("WITH `x` AS (SELECT 1) SELECT * FROM `x`", "snowflake")


def test_cte_name_may_contain_a_dollar_sign():
    # sql_scope's identifier alphabet admits "$" (Snowflake: x$1 is a legal identifier).
    _accepts("WITH x$1 AS (SELECT 1) SELECT * FROM x$1", "snowflake")


@pytest.mark.parametrize("engine", ENGINES)
def test_explain_of_a_write_is_admitted(engine):
    # Plan-only on Databricks and Snowflake; BigQuery has no EXPLAIN statement, so its dry run
    # returns a syntax error. Admitted is not supported: the gate refuses writes, it does not
    # validate reads, and EXPLAIN never executes the statement it plans.
    _accepts("EXPLAIN DELETE FROM t", engine)


# --- refused --------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "DELETE FROM t",
        "CREATE TABLE t AS SELECT 1",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
        "UPDATE t SET a = 1",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN a INT64",
        "TRUNCATE TABLE t",
        "GRANT SELECT ON t TO r",
        "CALL p()",
        "EXECUTE IMMEDIATE 'DELETE FROM t'",
        "BEGIN SELECT 1; END",
        "DECLARE x INT64",
        "SET x = 1",
        "USE WAREHOUSE w",
        "COPY INTO t FROM @stage",
        "EXPORT DATA OPTIONS(uri='gs://b/*') AS SELECT 1",
        "LOAD DATA INTO t FROM FILES (uris=['gs://b/f'])",
        "TABLE t",
        "FROM t SELECT 1",
        "-- c\n(INSERT INTO t VALUES (1))",
        "/* c */ ((DELETE FROM t))",
        "insert into t values (1)",
    ],
)
def test_refuses_non_read_statements_on_every_engine(sql, engine):
    message = _refuses(sql, engine)
    assert f"{ALLOW_WRITE_STATEMENTS_ENV_VAR}=1" in message
    assert "different keyword" in message


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
        "WITH RECURSIVE x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
        "WITH x AS (SELECT 1) DELETE FROM t USING x WHERE t.a = x.a",
        "WITH x AS (SELECT 1) UPDATE t SET a = 1",
        "WITH x AS (SELECT 1) MERGE INTO t USING x ON t.a = x.a WHEN MATCHED THEN DELETE",
    ],
)
def test_refuses_a_with_whose_main_statement_is_a_write(sql, engine):
    # Spark SQL: `ctes? dmlStatementNoWith` - one statement, first keyword WITH, effect a
    # write; a first-keyword check alone would admit it on Databricks.
    message = _refuses(sql, engine)
    assert "WITH clause" in message
    assert "SELECT or VALUES" in message


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1)",
        "WITH x (SELECT 1) SELECT 1",
        "WITH x AS SELECT 1",
        "WITH x AS MATERIALIZED (SELECT 1) SELECT 1",
        "WITH x AS (INSERT INTO t VALUES (1)) SELECT 1",
        "WITH x AS (DELETE FROM t) SELECT 1",
        "WITH x AS (WITH y AS (DELETE FROM t) SELECT 1) SELECT 1",
        "WITH x AS (SELECT 1) (DELETE FROM t)",
        "WITH x AS (SELECT 1), SELECT 1",
        "WITH 'x' AS (SELECT 1) SELECT 1",
        # A CTE literally named `recursive`: the bare word is read as the keyword, so the
        # walker looks for the CTE name after it and finds `AS` instead (safe direction).
        "WITH recursive AS (SELECT 1) SELECT * FROM recursive",
        # Text ends where a CTE name should start (after WITH, after a trailing comma).
        "WITH",
        "WITH x AS (SELECT 1),",
    ],
)
def test_refuses_a_with_the_walker_cannot_resolve(sql, engine):
    _refuses(sql, engine, match="WITH clause")


@pytest.mark.parametrize("engine", ENGINES)
def test_unbalanced_cte_body_is_refused(engine):
    _refuses("WITH x AS (SELECT 1 SELECT 2", engine, match="not closed")


# --- CTE nesting bound ----------------------------------------------------------------------


def _nested_with(levels: int) -> str:
    """``levels`` WITHs, each the only CTE body of the one outside it."""
    return "WITH a AS (" * levels + "SELECT 1" + ") SELECT 1" * levels


def _nested_with_in_parens(levels: int) -> str:
    """``levels`` WITHs, each the parenthesised main statement of the one outside it."""
    return "WITH x AS (SELECT 1) (" * levels + "SELECT 1" + ")" * levels


@pytest.mark.parametrize("engine", ENGINES)
def test_cte_nesting_at_the_bound_is_admitted_and_one_past_it_is_refused(engine):
    _accepts(_nested_with(_MAX_CTE_NESTING), engine)
    message = _refuses(_nested_with(_MAX_CTE_NESTING + 1), engine, match="nests CTEs")
    assert f"the WITH clause of the statement nests CTEs more than {_MAX_CTE_NESTING} levels" in (
        message
    )
    assert "rewrite without the nested CTEs" in message
    assert f"{ALLOW_WRITE_STATEMENTS_ENV_VAR}=1" in message


@pytest.mark.parametrize("engine", ENGINES)
def test_nesting_through_a_parenthesised_main_statement_counts_too(engine):
    # The walk also descends into "WITH ... (WITH ...)": the same recursion, the same bound.
    _accepts(_nested_with_in_parens(_MAX_CTE_NESTING), engine)
    _refuses(_nested_with_in_parens(_MAX_CTE_NESTING + 1), engine, match="nests CTEs")


def test_nesting_refusal_points_at_the_first_with_past_the_bound():
    offset = len("WITH a AS (") * _MAX_CTE_NESTING
    assert f"(at character {offset})" in _refuses(_nested_with(_MAX_CTE_NESTING + 1), "bigquery")


def test_nesting_refusal_names_the_statement_and_offsets_from_the_text_start(monkeypatch):
    monkeypatch.setenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, "1")
    prefix = "SELECT 1; "
    message = _refuses(prefix + _nested_with(_MAX_CTE_NESTING + 1), "bigquery")
    assert "the WITH clause of statement 2 nests CTEs" in message
    assert f"(at character {len(prefix) + len('WITH a AS (') * _MAX_CTE_NESTING})" in message


def test_deeply_nested_with_is_refused_fast_and_without_a_recursion_error():
    # Unbounded, the walk overran the interpreter's recursion limit near 500 levels - a
    # RecursionError the tool layer turned into the generic UnexpectedToolError instead of
    # this gate's labelled refusal - and re-scanned the enclosed text once per level, so a
    # 500 KB string cost ~47 s inside a run_query_bounded in-flight slot.
    sql = _nested_with(2000)
    # CPU time, not wall clock: the bound guards against the quadratic re-scan (tens of
    # seconds) and must not flake when a loaded CI runner deschedules the process.
    started = time.process_time()
    _refuses(sql, "bigquery", match="nests CTEs")
    assert time.process_time() - started < 5.0


@pytest.mark.parametrize("engine", [*ENGINES, "redshift"])
def test_comments_and_literals_open_only_at_the_lexer_opener_characters(engine):
    # The walker's loops skip any character outside LEXER_OPENERS without consulting the
    # lexer; this pins that no comment or literal form, on any dialect, starts elsewhere.
    dialect = dialect_for(engine)
    sample = "SELECT a, b_$1 FROM t WHERE x = 1.5e3 AND y IN (2, 3) ; -- # // /* ' \" ` $$ \\"
    sample += " \t\n\r\x0b\x0c\x85  @!%^&*+=<>?[]{}|~,:x'y\"z`w"
    for index, ch in enumerate(sample):
        if ch in LEXER_OPENERS:
            continue
        assert comment_end(sample, index, dialect) is None, (index, ch)
        assert literal_end(sample, index, dialect) is None, (index, ch)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("sql", ["'abc'", "1", "﻿SELECT 1", ";", "ſelect 1", "ＳＥＬＥＣＴ 1"])
def test_refuses_text_without_an_ascii_keyword(sql, engine):
    # str.upper() maps U+017F to "S": a Unicode-aware token would let "ſelect" pass. No engine
    # lexes a non-ASCII token as a keyword, so refusing it costs nothing.
    _refuses(sql, engine)


@pytest.mark.parametrize("engine", ENGINES)
def test_refusal_names_the_opt_out_and_never_echoes_the_sql(engine):
    message = _refuses("DELETE FROM secret_table", engine)
    assert "secret_table" not in message
    assert f"{ALLOW_WRITE_STATEMENTS_ENV_VAR}=1" in message
    assert "credential" in message
    assert "(at character 0)" in message


def test_refusal_offset_points_at_the_statement_keyword():
    message = _refuses("  /* c */ (\n DELETE FROM t)", "bigquery")
    assert "(at character 13)" in message


# --- under the multi-statement opt-out every statement must be a read ------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_multi_statement_text_is_checked_statement_by_statement(monkeypatch, engine):
    monkeypatch.setenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, "1")
    _accepts("SELECT 1; SELECT 2", engine)
    _accepts("SELECT 1; -- c\n; SELECT 2;", engine)
    message = _refuses("SELECT 1; DELETE FROM t", engine)
    assert "statement 2" in message
    assert "(at character 10)" in message
    _refuses("DECLARE x INT64; SELECT 1", engine)
    _refuses("SELECT 1; WITH x AS (SELECT 1) INSERT INTO t SELECT 1", engine, match="WITH clause")


def test_unscannable_text_is_refused_by_the_read_gate_too(monkeypatch):
    # The single-statement gate is skipped; the shared lexer's own refusal propagates.
    monkeypatch.setenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, "1")
    _refuses("SELECT 'x; DELETE FROM t", "bigquery", match="unterminated")
    _refuses("", "bigquery", match="no SQL statement")
    _refuses("-- only a comment\n", "bigquery", match="no SQL statement")


# --- operator opt-out -----------------------------------------------------------------------


def test_opt_out_env_var_set_to_1_skips_the_check_and_logs_it(monkeypatch, caplog):
    monkeypatch.setenv(ALLOW_WRITE_STATEMENTS_ENV_VAR, "1")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        for engine in ENGINES:
            _accepts("DELETE FROM t", engine)
    skipped = [r.message for r in caplog.records if "read_statement_check=skipped" in r.message]
    assert skipped == [
        f"engine={engine} read_statement_check=skipped reason={ALLOW_WRITE_STATEMENTS_ENV_VAR}=1"
        for engine in ENGINES
    ]


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "TRUE", " 1", "01"])
def test_opt_out_env_var_other_values_keep_the_check(monkeypatch, value):
    monkeypatch.setenv(ALLOW_WRITE_STATEMENTS_ENV_VAR, value)
    _refuses("DELETE FROM t", "bigquery")


def test_opt_out_is_read_at_call_time_not_import_time(monkeypatch):
    monkeypatch.delenv(ALLOW_WRITE_STATEMENTS_ENV_VAR, raising=False)
    _refuses("DELETE FROM t", "bigquery")
    monkeypatch.setenv(ALLOW_WRITE_STATEMENTS_ENV_VAR, "1")
    _accepts("DELETE FROM t", "bigquery")


def test_refusal_is_logged_once_and_success_is_silent(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        _accepts("SELECT 1", "bigquery")
        _refuses("DELETE FROM t", "bigquery")
    records = [r.message for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    assert records[0].startswith("engine=bigquery read_statement_check=refused detail=")


# --- first_keyword --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", ("SELECT", 0)),
        ("  (( -- c\n select 1", ("SELECT", 11)),
        ("SELECT'a'", ("SELECT", 0)),
        ("'a'", (None, 0)),
        ("", (None, 0)),
        ("/* c */", (None, 7)),
        ("SELECT$x", ("SELECT", 0)),
    ],
)
def test_first_keyword_returns_the_upper_cased_word_and_its_offset(sql, expected):
    assert first_keyword(sql, dialect_for("bigquery")) == expected


# --- #94: is the statement valid as the derived table of the LIMIT wrap? ---------------------


# The live gate's reproduction (#124): admitted by the read gate, refused by BigQuery inside the
# wrap, so it must reach the warehouse unwrapped.
_RECURSIVE_CTE = (
    "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT n FROM t"
)


def _nested_with(depth):
    sql = "SELECT 1"
    for _ in range(depth):
        sql = f"WITH t AS ({sql}) SELECT * FROM t"  # noqa: S608 - test text, never executed
    return sql


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", True),
        ("select 1;", True),
        ("(SELECT 1)", True),
        ("((SELECT 1))", True),
        ("-- c\nSELECT 1", True),
        ("/* c */ VALUES (1)", True),
        ("WITH t AS (SELECT 1) SELECT * FROM t", True),
        ("WITH t AS (SELECT 1) VALUES (1)", True),
        ("WITH t AS (SELECT 1), u AS (SELECT 2) (SELECT * FROM t)", True),
        (_nested_with(3), True),
        # A nested non-recursive WITH inside a CTE body leaves the main statement wrappable.
        ("WITH t AS (WITH u AS (SELECT 1) SELECT * FROM u) SELECT * FROM t", True),
        # #124: BigQuery allows WITH RECURSIVE only at the top level of a statement, so a
        # recursive main WITH is a read the engines must dispatch unwrapped; the fetch caps it.
        # Only the statement's own WITH counts, after comments, whitespace and parentheses.
        (_RECURSIVE_CTE, False),
        ("with recursive t as (select 1) select * from t", False),
        ("-- c\nWITH RECURSIVE t AS (SELECT 1) SELECT * FROM t", False),
        ("/* c */ WITH /* c */ RECURSIVE t AS (SELECT 1) SELECT * FROM t", False),
        ("(WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t)", False),
        ("((WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t))", False),
        ("WITH RECURSIVE t AS (SELECT 1) VALUES (1)", False),
        # RECURSIVE is a keyword only as the word right after WITH: a CTE named like it, or
        # a longer identifier, is an ordinary non-recursive WITH.
        ("WITH recursive_t AS (SELECT 1) SELECT * FROM recursive_t", True),
        ("WITH RECURSIVEX AS (SELECT 1) SELECT * FROM RECURSIVEX", True),
        # The bare word is taken as the keyword on every engine: a CTE literally named
        # `recursive` is not wrappable, and the read gate refuses the text anyway (after
        # skipping RECURSIVE the walker finds no `AS`; pinned below). Quoted names
        # (`RECURSIVE`, "RECURSIVE") are ordinary identifiers and stay wrappable.
        ("WITH recursive AS (SELECT 1) SELECT * FROM recursive", False),
        # A WITH is not enough: Spark allows WITH ... INSERT, and under the write opt-out the
        # gate does not look at it - wrapping it would be invalid SQL.
        ("WITH t AS (SELECT 1) INSERT INTO x SELECT * FROM t", False),
        ("WITH t AS (SELECT 1) MERGE INTO x USING t ON 1=1", False),
        ("WITH RECURSIVE t AS (SELECT 1) INSERT INTO x SELECT * FROM t", False),
        ("SHOW TABLES", False),
        ("DESCRIBE t", False),
        ("DESC t", False),
        ("EXPLAIN SELECT 1", False),
        ("INSERT INTO t VALUES (1)", False),
        ("DELETE FROM t", False),
        ("SELECT 1; SELECT 2", False),
        ("", False),
        ("-- only", False),
        ("'x'", False),
        ("WITH t AS (SELECT 1", False),  # unbalanced: the walker refuses -> False, no raise
        (_nested_with(_MAX_CTE_NESTING + 2), False),  # too deep: refused -> False, no raise
    ],
)
def test_is_single_query_statement_truth_table(sql, expected, engine):
    assert is_single_query_statement(sql, engine) is expected  # type: ignore[arg-type]


def test_is_single_query_statement_never_logs(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        is_single_query_statement("SELECT 1; SELECT 2", "bigquery")
        is_single_query_statement(_nested_with(_MAX_CTE_NESTING + 2), "bigquery")
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
