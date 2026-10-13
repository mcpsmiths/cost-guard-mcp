import pytest

from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.sql_scope import (
    ALLOW_MULTI_STATEMENT_SQL_ENV_VAR,
    assert_single_statement,
    single_statement_body,
)

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


# --- fix-up review: line-comment terminators and doubled-delimiter ambiguity -------------

# Every character str.splitlines() treats as a line boundary. Spark's lexer ends a `--`
# comment at a bare '\r' and ZetaSQL's at any of [\r\n]; the scanner must end a line comment
# no later than any engine does, or the text after the engine's terminator hides a statement.
_LINE_TERMINATORS = [
    "\n",
    "\r",
    "\r\n",
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("terminator", _LINE_TERMINATORS)
def test_line_comment_ends_at_every_line_terminator_not_only_newline(engine, terminator):
    # BigQuery runs ';'-separated text as a script: a comment the scanner reads to the end of
    # the text but the engine ends at '\r' hides `DROP TABLE t` behind an estimate of SELECT 1.
    _refuses(f"SELECT 1 -- x{terminator}; DROP TABLE t", engine)
    _refuses(f"SELECT 1; -- x{terminator}DROP TABLE t", engine)


@pytest.mark.parametrize("engine", ENGINES)
def test_hash_line_comment_ends_at_a_carriage_return(engine):
    # bigquery: the '#' comment ends at '\r' and the DROP is a second statement; snowflake and
    # databricks have no '#' comment at all, so the top-level ';' is reached directly.
    _refuses("SELECT 1 # x\r; DROP TABLE t", engine)


def test_double_slash_line_comment_ends_at_a_carriage_return_for_snowflake():
    _refuses("SELECT 1 // x\r; DROP TABLE t", "snowflake")


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("terminator", _LINE_TERMINATORS)
def test_line_comment_closed_by_any_line_terminator_is_still_one_statement(engine, terminator):
    _accepts(f"SELECT 1 -- x{terminator}", engine)
    _accepts(f"SELECT 1 -- x{terminator}  ; -- y{terminator}", engine)


@pytest.mark.parametrize("engine", ["bigquery", "databricks"])
def test_raw_string_backslash_before_a_doubled_closing_quote_is_refused(engine):
    # Under the escape-honouring reading (ZetaSQL lexes r'..' with the cooked string's escape
    # rules) \' does not close the literal, so it ends at the second quote and `; DROP TABLE
    # t` is top level; under the escape-free reading '' is a doubled quote inside the body.
    # The doubled pair must not hide the odd backslash run: refuse rather than guess.
    _refuses("SELECT r'\\''; DROP TABLE t; --'", engine, match="ambiguous")
    _refuses('SELECT r"\\""; DROP TABLE t; --"', engine, match="ambiguous")


def test_raw_bytes_backslash_before_a_doubled_closing_quote_is_refused_for_bigquery():
    _refuses("SELECT rb'\\''; DROP TABLE t; --'", "bigquery", match="ambiguous")


@pytest.mark.parametrize("engine", ["bigquery", "databricks"])
def test_backtick_identifier_backslash_before_a_doubled_backtick_is_refused(engine):
    _refuses("SELECT `a\\``; DROP TABLE t; --` FROM x", engine, match="ambiguous")


def test_hex_literal_backslash_before_a_doubled_closing_quote_is_refused_for_databricks():
    _refuses("SELECT X'0A\\''; DROP TABLE t; --'", "databricks", match="ambiguous")


@pytest.mark.parametrize("engine", ["bigquery", "databricks"])
def test_doubled_delimiters_without_a_backslash_still_read_as_one_literal(engine):
    _accepts("SELECT r'a''b'", engine)
    _accepts("SELECT `a``b` FROM t", engine)
    _accepts("SELECT r'a''b'; -- trailing", engine)
    # An even run before the doubled pair: both readings agree it is two literal backslashes.
    _accepts("SELECT r'\\\\''b'", engine)


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


# --- single_statement_body: what the engines wrap in the LIMIT subquery -------------------


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    ("sql", "body"),
    [
        ("SELECT 1", "SELECT 1"),
        ("SELECT 1;", "SELECT 1"),
        ("  \n SELECT 1 ;\n  ", "SELECT 1"),
        # A trailing line comment used to survive `rstrip(";")` and swallow the wrapper's
        # closing parenthesis: `SELECT * FROM (SELECT 1 -- c) AS cost_guard_row_cap LIMIT 6`.
        ("SELECT 1 -- c", "SELECT 1"),
        ("SELECT 1; -- c", "SELECT 1"),
        ("SELECT 1;\n  /* c; */ \n", "SELECT 1"),
        ("SELECT 1 /* c */ ;", "SELECT 1"),
        ("SELECT 1 -- c\n;", "SELECT 1"),
        ("SELECT ';' AS s;", "SELECT ';' AS s"),
        ("SELECT '--x' AS s -- c", "SELECT '--x' AS s"),
        ("SELECT 1 /* a */ + /* b */ 2", "SELECT 1 /* a */ + /* b */ 2"),
    ],
)
def test_single_statement_body_drops_the_trailing_terminator_whitespace_and_comments(
    sql, body, engine
):
    assert single_statement_body(sql, engine) == body  # type: ignore[arg-type]


def test_single_statement_body_honours_each_dialects_line_comment_forms():
    assert single_statement_body("SELECT 1 # c", "bigquery") == "SELECT 1"
    assert single_statement_body("SELECT 1 // c", "snowflake") == "SELECT 1"
    # Neither is a comment in Spark SQL: the text stays for the provider to judge.
    assert single_statement_body("SELECT 1 # c", "databricks") == "SELECT 1 # c"
    assert single_statement_body("SELECT 1 // c", "databricks") == "SELECT 1 // c"
    assert single_statement_body("SELECT `a#b` FROM t -- c", "databricks") == "SELECT `a#b` FROM t"


def test_single_statement_body_keeps_the_old_semicolon_strip_when_the_text_is_unscannable(
    monkeypatch,
):
    # The gate (assert_single_statement) is a separate, earlier call; this helper never
    # refuses. With the operator opt-out set, multi-statement or unscannable text reaches
    # the engines and only the pre-existing best-effort strip applies.
    monkeypatch.setenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, "1")
    assert single_statement_body("SELECT 'x;", "bigquery") == "SELECT 'x"
    assert single_statement_body("SELECT 1; SELECT 2;", "bigquery") == "SELECT 1; SELECT 2"


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("sql", ["", "   ", "-- c\n", "/* c */", ";", "-- c\n;", "/* a */ ; -- b"])
def test_refuses_text_with_no_sql_statement(sql, engine):
    # PR #86 delta verification: comment-only, whitespace-only and lone-';' texts passed the
    # gate, the estimate was dispatched on the raw text and, with max_rows, an empty subquery
    # 'SELECT * FROM () AS cost_guard_row_cap LIMIT n' reached the provider.
    _refuses(sql, engine, match="no SQL statement")
