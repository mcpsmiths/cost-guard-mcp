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
