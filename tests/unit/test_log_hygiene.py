"""Tests for the third-party logger pins and the redacting root-handler filter (#95).

The mcp SDK's MCPServer.__init__ runs logging.basicConfig on the root logger at INFO with one
stderr StreamHandler, so every warehouse-client library logger inherits INFO and its records
reach stderr without ever passing redact_secrets. configure_log_hygiene() pins the named
third-party loggers to WARNING and adds a RedactingLogFilter to every root handler.

Logger levels and root-handler filters are process-global; the autouse fixture below restores
both after each test so nothing here leaks into the rest of the session.
"""

import io
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp import log_hygiene
from cost_guard_mcp.log_hygiene import (
    THIRD_PARTY_LOGGERS,
    RedactingLogFilter,
    configure_log_hygiene,
)

_PROJECT_LOGGER_NAME = "cost_guard_mcp.errors"


def _dummy_secret(label: str) -> str:
    # Assembled at runtime so no assignment-shaped secret literal appears in this file.
    return label.upper() + "-" + "hunter" + "2" * 6


@pytest.fixture(autouse=True)
def _restore_logging_state():
    root = logging.getLogger()
    saved_levels = {name: logging.getLogger(name).level for name in THIRD_PARTY_LOGGERS}
    saved_filters = {id(handler): list(handler.filters) for handler in root.handlers}
    yield
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)
    for handler in root.handlers:
        if id(handler) in saved_filters:
            handler.filters = saved_filters[id(handler)]


@pytest.fixture
def root_stream():
    """A plain StreamHandler on the root logger standing in for the SDK's own, with its
    output captured; removed again after the test."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream, handler
    finally:
        root.removeHandler(handler)


def test_third_party_loggers_is_exactly_the_reviewed_list():
    # The owner-confirmed list; adding or removing a name is a reviewed diff, not a drift.
    assert THIRD_PARTY_LOGGERS == (
        "snowflake.connector",
        "databricks",
        "databricks.sql",
        "databricks.sdk",
        "google",
        "urllib3",
        "httpx",
        "httpcore",
    )


def test_configure_pins_each_third_party_logger_to_warning_and_leaves_the_project_logger():
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    configure_log_hygiene()

    for name in THIRD_PARTY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name
    # The project's own records keep the SDK's level: nothing here touches cost_guard_mcp.
    assert logging.getLogger("cost_guard_mcp").level == logging.NOTSET


def test_configure_attaches_one_redacting_filter_per_root_handler_and_is_idempotent(root_stream):
    _, handler = root_stream

    configure_log_hygiene()
    configure_log_hygiene()

    for root_handler in logging.getLogger().handlers:
        redacting = [f for f in root_handler.filters if isinstance(f, RedactingLogFilter)]
        assert len(redacting) == 1, type(root_handler).__name__
    assert handler in logging.getLogger().handlers


def test_snowflake_connector_info_is_not_emitted_but_warning_and_project_info_are(caplog):
    # Issue #95 acceptance criteria, verbatim.
    configure_log_hygiene()
    with caplog.at_level(logging.INFO):
        logging.getLogger("snowflake.connector").info("connector info chatter")
        logging.getLogger("snowflake.connector").warning("connector warning")
        logging.getLogger(_PROJECT_LOGGER_NAME).info("tool=x outcome=success elapsed_ms=1.0")

    messages = [(r.name, r.levelno, r.getMessage()) for r in caplog.records]
    assert ("snowflake.connector", logging.INFO, "connector info chatter") not in messages
    assert ("snowflake.connector", logging.WARNING, "connector warning") in messages
    assert (
        _PROJECT_LOGGER_NAME,
        logging.INFO,
        "tool=x outcome=success elapsed_ms=1.0",
    ) in messages


def test_redacting_filter_redacts_message_and_args_rendered_once():
    secret = _dummy_secret("sf")
    record = logging.LogRecord(
        "snowflake.connector",
        logging.WARNING,
        __file__,
        1,
        "login failed for %s: " + "pass" + "word=%s",
        ("svc_user", secret),
        None,
    )

    assert RedactingLogFilter().filter(record) is True

    assert record.args is None  # rendered once; the Formatter must not run `%` again
    assert secret not in record.getMessage()
    assert "REDACTED" in record.getMessage()
    assert "svc_user" in record.getMessage()


def test_redacting_filter_never_raises_or_drops_an_unrenderable_record():
    # A %-format/args mismatch makes getMessage() raise; the stdlib would then print the raw
    # msg and args through Handler.handleError. The filter must neither raise nor drop.
    record = logging.LogRecord(
        "google.auth", logging.WARNING, __file__, 1, "%d retries left", ("not-a-number",), None
    )

    assert RedactingLogFilter().filter(record) is True

    assert record.args is None
    rendered = record.getMessage()
    assert "not-a-number" not in rendered
    assert rendered == log_hygiene._UNRENDERABLE_RECORD_MESSAGE


def test_redacting_filter_leaves_a_secret_free_record_readable():
    record = logging.LogRecord(
        "urllib3", logging.WARNING, __file__, 1, "Retrying (%d) after %s", (2, "timeout"), None
    )

    assert RedactingLogFilter().filter(record) is True
    assert record.getMessage() == "Retrying (2) after timeout"


def test_root_stream_handler_emits_only_the_redacted_third_party_warning(root_stream):
    stream, _ = root_stream
    secret = _dummy_secret("dbx")
    configure_log_hygiene()

    databricks = logging.getLogger("databricks.sql")
    databricks.info("opening session with " + "tok" + "en=%s", secret)
    databricks.warning("session failed, " + "tok" + "en=%s", secret)

    output = stream.getvalue()
    assert secret not in output
    assert "opening session" not in output
    assert "session failed, token=***REDACTED***" in output


def test_filter_covers_a_record_logged_through_the_project_logger_too(root_stream):
    # Defence in depth, not a replacement: a project record that somehow carried a secret is
    # redacted at the handler as well.
    stream, _ = root_stream
    secret = _dummy_secret("bq")
    configure_log_hygiene()

    logging.getLogger(_PROJECT_LOGGER_NAME).warning("engine=bigquery " + "pass" + "word=%s", secret)

    assert secret not in stream.getvalue()
    assert "engine=bigquery password=***REDACTED***" in stream.getvalue()


# --- exception traceback and stack text: appended by the Formatter, not part of getMessage() --


def _record_with_exc_info(exc: Exception, message: str = "call failed") -> logging.LogRecord:
    try:
        raise exc
    except type(exc):  # raised and caught so the record carries a real traceback
        exc_info = sys.exc_info()
    return logging.LogRecord(
        "snowflake.connector", logging.WARNING, __file__, 1, message, None, exc_info
    )


def test_root_stream_handler_redacts_the_traceback_of_a_third_party_warning_with_exc_info(
    root_stream,
):
    # snowflake-connector-python #1323's shape: the credential is in the exception text and
    # the library logs it with exc_info=True, so it travels in the traceback the Formatter
    # appends - never through getMessage().
    stream, _ = root_stream
    secret = _dummy_secret("sf")
    configure_log_hygiene()

    try:
        raise RuntimeError("250001: login failed, " + "pass" + "word=" + secret)
    except RuntimeError:
        logging.getLogger("snowflake.connector.network").warning(
            "connector failure during login", exc_info=True
        )

    output = stream.getvalue()
    assert secret not in output
    assert "connector failure during login" in output
    assert "Traceback (most recent call last)" in output
    assert "RuntimeError: 250001: login failed, password=***REDACTED***" in output


def test_root_stream_handler_redacts_the_traceback_logged_by_logger_exception(root_stream):
    stream, _ = root_stream
    secret = _dummy_secret("dbx")
    configure_log_hygiene()

    try:
        raise ValueError("Authorization: " + "tok" + "en=" + secret)
    except ValueError:
        logging.getLogger("databricks.sdk").exception("request failed")

    output = stream.getvalue()
    assert secret not in output
    assert "request failed" in output
    assert "ValueError: Authorization: token=***REDACTED***" in output


def test_root_stream_handler_redacts_a_record_logged_with_stack_info(root_stream):
    stream, _ = root_stream
    secret = _dummy_secret("bq")
    configure_log_hygiene()

    logging.getLogger("google.auth").warning(
        "refresh failed, " + "pass" + "word=%s", secret, stack_info=True
    )

    output = stream.getvalue()
    assert secret not in output
    assert "refresh failed, password=***REDACTED***" in output
    assert "Stack (most recent call last)" in output


def test_redacting_filter_redacts_stack_text_and_moves_exc_info_into_redacted_exc_text():
    secret = _dummy_secret("sf")
    record = _record_with_exc_info(RuntimeError("login failed, " + "pass" + "word=" + secret))
    record.stack_info = (
        "Stack (most recent call last):\n  File x, line 1\n    " + "tok" + "en=" + secret
    )

    assert RedactingLogFilter().filter(record) is True

    # exc_info is cleared so no handler can render the raw traceback again from it - the
    # stdlib Formatter reuses exc_text, but rich's RichHandler (which the SDK installs when
    # `rich` is importable) renders exc_info itself and ignores exc_text.
    assert record.exc_info is None
    assert record.exc_text is not None
    assert secret not in record.exc_text
    assert record.exc_text.startswith("Traceback (most recent call last)")
    assert "RuntimeError: login failed, password=***REDACTED***" in record.exc_text
    assert record.stack_info is not None
    assert secret not in record.stack_info
    assert "token=***REDACTED***" in record.stack_info


def test_redacting_filter_redacts_a_cached_exc_text_that_arrived_without_exc_info():
    # A handler earlier in the chain (one on a child logger, without this filter) may already
    # have run its Formatter and cached the raw traceback on record.exc_text.
    secret = _dummy_secret("dbx")
    record = logging.LogRecord("databricks", logging.ERROR, __file__, 1, "failed", None, None)
    record.exc_text = "Traceback ...\nValueError: " + "tok" + "en=" + secret

    assert RedactingLogFilter().filter(record) is True

    assert secret not in (record.exc_text or "")
    assert "ValueError: token=***REDACTED***" in (record.exc_text or "")


def test_redacting_filter_renders_a_traceback_whose_exception_str_raises_without_raising():
    class StrRaises(Exception):
        def __str__(self) -> str:
            raise RuntimeError("__str__ failed")

    record = _record_with_exc_info(StrRaises(), message="connector failure")

    assert RedactingLogFilter().filter(record) is True

    # The record is still emitted with its message; the stdlib renders the exception line as
    # "<exception str() failed>" rather than raising, and nothing here changes that.
    assert record.getMessage() == "connector failure"
    assert record.exc_info is None
    assert record.exc_text is not None
    assert "Traceback (most recent call last)" in record.exc_text


def test_redacting_filter_withholds_the_whole_record_when_the_traceback_cannot_be_rendered():
    # A malformed exc_info (a bare True, as a hand-built record or a buggy wrapper can carry)
    # makes Formatter.formatException raise. The filter must neither raise nor drop; it
    # withholds the text and clears every field a handler could render raw.
    secret = _dummy_secret("sf")
    record = logging.LogRecord(
        "snowflake.connector",
        logging.WARNING,
        __file__,
        1,
        "login " + "pass" + "word=%s",
        (secret,),
        None,
    )
    record.exc_info = True  # type: ignore[assignment]
    record.stack_info = "Stack: " + "tok" + "en=" + secret

    assert RedactingLogFilter().filter(record) is True

    assert record.getMessage() == log_hygiene._UNRENDERABLE_RECORD_MESSAGE
    assert record.args is None
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None


def test_main_configures_log_hygiene_before_running_the_server():
    from cost_guard_mcp import server

    order = MagicMock()
    with (
        patch.object(server, "configure_log_hygiene", order.hygiene),
        patch.object(server, "_bootstrap_opentelemetry", order.otel),
        patch.object(server, "mcp", order.mcp),
    ):
        server.main()

    assert [call[0] for call in order.mock_calls] == ["hygiene", "otel", "mcp.run"]
