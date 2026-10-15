"""Keep the warehouse-client libraries' log records off stderr, and redact what gets through.

The mcp SDK's ``MCPServer.__init__`` (``mcp/server/mcpserver/server.py``, ``configure_logging``
in ``mcp/server/mcpserver/utilities/logging.py``) runs ``logging.basicConfig`` on the ROOT
logger at INFO with one stderr handler the moment ``server.py`` constructs the server. Every
library logger - ``snowflake.connector``, ``databricks.*``, ``google.*`` and the HTTP stacks
under them - is NOTSET, so it inherits INFO and its records land on that same stderr without
ever passing ``redact_secrets``, which only runs on this project's own ``sanitize_exceptions``
path. README's "never logs a credential" claim therefore covered only this project's records.

Two measures, both applied once by :func:`configure_log_hygiene` at server start:

1. The loggers in :data:`THIRD_PARTY_LOGGERS` are pinned to WARNING. Their INFO chatter (every
   connection, every HTTP request line, every token refresh) is noise to an operator reading a
   stdio server's stderr and is the volume in which an embedded credential hides; their
   WARNING and ERROR records - the ones that carry an exception's text - still arrive.
2. A :class:`RedactingLogFilter` is attached to every handler on the root logger, so every
   record that does reach stderr, whoever logged it, has its message, its exception traceback
   and its stack text rendered once and passed through ``redact_secrets`` first. Defence in
   depth behind the pins and behind ``sanitize_exceptions``, not a replacement for either.

The project's own ``cost_guard_mcp`` logger is deliberately not touched: it keeps the SDK's
level, so the ``tool=<name> outcome=`` INFO records operators key on keep flowing.
"""

import logging
from typing import Final

from cost_guard_mcp.errors import redact_secrets

# Logger names pinned to WARNING at server start. Exactly these; one reason per line.
THIRD_PARTY_LOGGERS: Final[tuple[str, ...]] = (
    # Confirmed history of credential text in connector exceptions (snowflake-connector-python
    # #1323); logs every login and statement at INFO.
    "snowflake.connector",
    # Parent of the two Databricks loggers below: pinned so a future databricks.* child that
    # neither of them covers inherits WARNING too.
    "databricks",
    # databricks-sql-connector: logs session open/close and every statement at INFO.
    "databricks.sql",
    # databricks-sdk: logs the Warehouses API calls and OAuth/PAT auth resolution at INFO.
    "databricks.sdk",
    # Parent of google.cloud.bigquery, google.auth and google.api_core: credential refreshes
    # and retry chatter at INFO.
    "google",
    # HTTP stack under google-auth/requests and databricks-sql: connection and retry lines
    # that quote the URL.
    "urllib3",
    # Not pulled in by this project's dependencies: the mcp SDK (2.3.0) depends on httpx2,
    # whose request line ("HTTP Request: GET https://...") logs under the logger name
    # "httpx2" (httpx2/_client.py), and databricks-sdk uses requests over urllib3. Kept as a
    # guard for a future transitive dependency that brings httpx in: pinning a logger no
    # library writes to costs nothing. Whether to add "httpx2"/"httpcore2" is the owner's call.
    "httpx",
    # httpx's transport (connection-level trace lines); same standing as httpx - a guard, not a
    # current dependency (the SDK's transport logs under "httpcore2.*").
    "httpcore",
)

# Replaces a record's text when msg % args cannot be rendered (a format/args mismatch in some
# library's logging call). Left alone, the record would go down Handler.handleError, which
# prints the raw msg and args to stderr - the exact path this filter exists to close.
_UNRENDERABLE_RECORD_MESSAGE: Final = "<log record withheld: message could not be rendered>"


# Renders record.exc_info the way the stdlib Formatter would (traceback.print_exception), so
# the text the handler appends is the text that was redacted. A bare Formatter: format strings
# play no part in formatException.
_TRACEBACK_FORMATTER: Final = logging.Formatter()


class RedactingLogFilter(logging.Filter):
    """Render each record's text once - message, exception traceback, stack text - redact it,
    and store it back on the record.

    A record's text reaches the handler by three routes and the Formatter concatenates them:
    ``getMessage()`` (``msg % args``), the traceback it renders from ``exc_info`` (cached on
    ``exc_text``), and ``stack_info``. Libraries put credential text in exception messages and
    log them with ``exc_info=True`` / ``logger.exception`` (snowflake-connector-python #1323),
    so redacting the message alone leaves the traceback raw. Here all three are rendered and
    redacted; ``exc_info`` is then cleared so no handler can render the raw traceback again
    from it - the stdlib Formatter reuses ``exc_text``, but rich's ``RichHandler`` (which the
    SDK installs when ``rich`` is importable) renders ``exc_info`` itself and ignores
    ``exc_text``. ``redact_secrets`` bounds its input (16 KiB, then a truncation marker), so a
    long traceback loses its tail, never its redaction.

    Never drops a record (always returns True) and never raises: ``redact_secrets`` is pure
    regex over a bounded ``str``, and the calls that can raise - ``getMessage()`` on a bad
    %-format, ``formatException`` on a malformed ``exc_info`` - are caught and the whole
    record's text replaced by a fixed placeholder, with every field a handler could render raw
    cleared. ``record.args`` is set to None so the handler's Formatter does a plain string
    lookup instead of a second ``%`` pass over text that may now contain a literal ``%``.
    Mutating the record in place is the stdlib's documented filter contract, and it is what
    makes the redaction hold for every handler the record is then offered to.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_secrets(record.getMessage())
            if record.exc_info:
                record.exc_text = _TRACEBACK_FORMATTER.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_secrets(record.exc_text)
            if record.stack_info:
                record.stack_info = redact_secrets(record.stack_info)
        except Exception:  # noqa: BLE001 - any failure here must neither raise nor drop
            record.msg = _UNRENDERABLE_RECORD_MESSAGE
            record.exc_text = None
            record.stack_info = None
        record.args = None
        record.exc_info = None
        return True


def configure_log_hygiene() -> None:
    """Pin the third-party loggers to WARNING and add the redacting filter to every root
    handler. Idempotent; ``server.py`` calls it at module scope directly after constructing
    ``MCPServer`` - the SDK installs its handler in that constructor, and no library record can
    be emitted before a tool body runs - so every launch path that imports the module (the
    console script, ``mcp run``/``mcp dev``, a host importing ``server.mcp``, in-process
    ``Client(mcp)`` tests) is covered, not only ``main()``.

    ``logging.getLogger(name)`` returns the same singleton the library itself holds, whether
    or not the library has been imported yet, so the pins do not depend on import order.
    Handler-level filters, not a root-logger filter: ``Logger.filter`` only sees records
    logged to that logger directly, never records propagated up from a child.
    """
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(existing, RedactingLogFilter) for existing in handler.filters):
            handler.addFilter(RedactingLogFilter())
