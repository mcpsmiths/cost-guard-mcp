import pytest

from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.sql_scope import ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, assert_single_statement

ENGINES = ["bigquery", "snowflake", "databricks"]

_ONE_STATEMENT = "exactly one SQL statement"


def _refuses(sql: str, engine: str, match: str = _ONE_STATEMENT) -> None:
    with pytest.raises(UserVisibleError, match=match):
        assert_single_statement(sql, engine)  # type: ignore[arg-type]


def _accepts(sql: str, engine: str) -> None:
    assert_single_statement(sql, engine)  # type: ignore[arg-type]


@pytest.mark.parametrize("engine", ENGINES)
def test_refuses_second_statement_after_top_level_semicolon(engine):
    _refuses("SELECT 1; DELETE FROM t", engine)


@pytest.mark.parametrize("engine", ENGINES)
def test_refusal_message_does_not_echo_the_sql_text(engine):
    with pytest.raises(UserVisibleError) as excinfo:
        assert_single_statement("SELECT 1; DELETE FROM secret_table", engine)
    assert "secret_table" not in str(excinfo.value)
    assert ALLOW_MULTI_STATEMENT_SQL_ENV_VAR in str(excinfo.value)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1;",
        "  \n SELECT 1 ;\n  ",
        "SELECT 1; -- trailing comment",
        "SELECT 1;\n  /* trailing block comment; with a semicolon */ \n",
        "SELECT 1 /* ; */",
        "SELECT ';' AS s",
        "SELECT 'it''s; fine'",
        'SELECT "a;b" FROM t',
        "SELECT 1 -- ; DELETE FROM t\n",
        # A backslash-escaped quote keeps a single-quoted string open in all three dialects.
        "SELECT 'a\\'; DELETE FROM t'",
    ],
)
def test_accepts_single_statement_shapes_shared_by_every_dialect(sql, engine):
    _accepts(sql, engine)


# --- engine-specific constructs: recognised only where the dialect has them -------------


def test_hash_line_comment_is_bigquery_only():
    _accepts("SELECT 1 # ; DELETE FROM t", "bigquery")
    _refuses("SELECT 1 # ; DELETE FROM t", "snowflake")
    _refuses("SELECT 1 # ; DELETE FROM t", "databricks")


def test_dollar_quoted_string_is_snowflake_only():
    _accepts("SELECT $$a;b$$", "snowflake")
    _refuses("SELECT $$a;b$$", "bigquery")
    _refuses("SELECT $$a;b$$", "databricks")


def test_backtick_identifier_is_bigquery_and_databricks_only():
    _accepts("SELECT `a;b` FROM t", "bigquery")
    _accepts("SELECT `a;b` FROM t", "databricks")
    _refuses("SELECT `a;b` FROM t", "snowflake")


def test_double_slash_line_comment_is_snowflake_only():
    _accepts("SELECT 1 // ; DELETE", "snowflake")
    _refuses("SELECT 1 // ; DELETE", "bigquery")
    _refuses("SELECT 1 // ; DELETE", "databricks")


def test_triple_quoted_string_is_bigquery_only():
    _accepts("SELECT '''a;b'''", "bigquery")
    _accepts('SELECT """a;b"""', "bigquery")
    _accepts("SELECT '''a'; DROP TABLE t; --'''", "bigquery")
    # Without triple quotes the text is '' + 'a' + top-level '; DROP TABLE t; --' + '''.
    _refuses("SELECT '''a'; DROP TABLE t; --'''", "databricks")
    _refuses("SELECT '''a'; DROP TABLE t; --'''", "snowflake")


def test_snowflake_double_quoted_identifier_has_no_backslash_escape():
    # Snowflake doubles the quote inside a quoted identifier; a backslash is an ordinary
    # character, so the identifier closes at the second quote and the DROP is top level.
    _refuses('SELECT "a\\"; DROP TABLE t; --"', "snowflake")
    _accepts('SELECT "a""b;c" FROM t', "snowflake")
    # BigQuery and Databricks read "..." as a string literal with backslash escapes.
    _accepts('SELECT "a\\"; DROP TABLE t; --"', "bigquery")
    _accepts('SELECT "a\\"; DROP TABLE t; --"', "databricks")


# --- bypass shapes from the plan review (amendment A1) ---------------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_raw_string_with_backslash_before_closing_quote_is_refused(engine):
    # BigQuery/Databricks raw strings do not process escapes, so the literal closes at the
    # second quote and `DROP TABLE t` is a second statement; Snowflake has no r prefix at
    # all. Every dialect must refuse.
    _refuses("SELECT r'\\'; DROP TABLE t; --'", engine)
    _refuses('SELECT R"\\"; DROP TABLE t; --"', engine)


def test_raw_bytes_prefix_with_backslash_before_closing_quote_is_refused_for_bigquery():
    _refuses("SELECT rb'\\'; DROP TABLE t; --'", "bigquery")
    _refuses("SELECT bR'\\'; DROP TABLE t; --'", "bigquery")


def test_cooked_bytes_literal_keeps_backslash_escape_for_bigquery_only():
    _accepts("SELECT b'\\'; DROP TABLE t; --'", "bigquery")
    _refuses("SELECT b'\\'; DROP TABLE t; --'", "databricks")
    _refuses("SELECT b'\\'; DROP TABLE t; --'", "snowflake")


@pytest.mark.parametrize("engine", ENGINES)
def test_semicolon_hidden_in_block_comment_then_second_statement_is_refused(engine):
    _refuses("SELECT 1 /*;*/; SELECT 2", engine)


@pytest.mark.parametrize("engine", ENGINES)
def test_two_terminators_are_refused(engine):
    _refuses("SELECT 1;;", engine)


# --- fail closed on anything the scanner cannot resolve ---------------------------------


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("sql", ["SELECT 'abc", 'SELECT "abc', "SELECT 1 /* open"])
def test_unterminated_literal_or_comment_is_refused(sql, engine):
    _refuses(sql, engine, match="unterminated")


def test_unterminated_engine_specific_constructs_are_refused():
    _refuses("SELECT `abc", "bigquery", match="unterminated")
    _refuses("SELECT `abc", "databricks", match="unterminated")
    _refuses("SELECT $$abc", "snowflake", match="unterminated")
    _refuses("SELECT '''abc", "bigquery", match="unterminated")


@pytest.mark.parametrize("engine", ENGINES)
def test_unknown_prefix_letters_attached_to_a_quote_are_refused_with_a_whitespace_hint(engine):
    _refuses("SELECT CASE WHEN a THEN'x' END FROM t", engine, match="whitespace")
    _refuses("SELECT DATE'2020-01-01'", engine, match="whitespace")
    _accepts("SELECT CASE WHEN a THEN 'x' END FROM t", engine)


@pytest.mark.parametrize("engine", ["bigquery", "databricks"])
def test_raw_string_regex_bodies_are_accepted_unless_the_closing_quote_is_ambiguous(engine):
    _accepts("SELECT REGEXP_CONTAINS(x, r'\\d+;\\w')", engine)
    _accepts("SELECT r'\\\\'", engine)  # even run of backslashes: both readings agree
    _refuses("SELECT r'a\\'", engine, match="ambiguous")  # odd run: readings diverge


def test_databricks_hex_literal_is_accepted_and_bigquery_snowflake_refuse_the_prefix():
    _accepts("SELECT X'0AFF'", "databricks")
    _refuses("SELECT X'0AFF'", "bigquery", match="whitespace")
    _refuses("SELECT X'0AFF'", "snowflake", match="whitespace")
    _refuses("SELECT X'0A\\'", "databricks", match="ambiguous")


@pytest.mark.parametrize("engine", ENGINES)
def test_block_comment_opener_inside_a_block_comment_is_refused(engine):
    # Nested block comments are dialect-specific; both readings can be made to disagree
    # about where the comment ends, so refuse rather than pick one.
    _refuses("SELECT 1 /* a /* b */ */", engine, match="nested")


def test_dollar_string_attached_to_an_identifier_is_refused_for_snowflake():
    _refuses("SELECT a$$b;c$$", "snowflake", match="ambiguous")


def test_unknown_engine_falls_back_to_the_shared_constructs_only():
    _accepts("SELECT 1", "redshift")
    _refuses("SELECT 1; DELETE FROM t", "redshift")


# --- operator opt-out ---------------------------------------------------------------------


def test_opt_out_env_var_set_to_1_skips_the_check(monkeypatch):
    monkeypatch.setenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, "1")
    for engine in ENGINES:
        _accepts("SELECT 1; DELETE FROM t", engine)


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "TRUE"])
def test_opt_out_env_var_other_values_keep_the_check(monkeypatch, value):
    monkeypatch.setenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, value)
    _refuses("SELECT 1; DELETE FROM t", "bigquery")


def test_opt_out_is_read_at_call_time_not_import_time(monkeypatch):
    monkeypatch.delenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, raising=False)
    _refuses("SELECT 1; DELETE FROM t", "bigquery")
    monkeypatch.setenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, "1")
    _accepts("SELECT 1; DELETE FROM t", "bigquery")
