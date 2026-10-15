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
   record that does reach stderr, whoever logged it, is rendered once and passed through
   ``redact_secrets`` first. Defence in depth behind the pins and behind
   ``sanitize_exceptions``, not a replacement for either.

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
    # HTTP stack under the mcp SDK and databricks-sdk: logs every request line
    # ("HTTP Request: GET https://...") at INFO, URL query string included.
    "httpx",
    # httpx's transport: connection-level trace lines.
    "httpcore",
)

# Replaces a record's text when msg % args cannot be rendered (a format/args mismatch in some
# library's logging call). Left alone, the record would go down Handler.handleError, which
# prints the raw msg and args to stderr - the exact path this filter exists to close.
_UNRENDERABLE_RECORD_MESSAGE: Final = "<log record withheld: message could not be rendered>"


class RedactingLogFilter(logging.Filter):
    """Render each record's message once, redact it, and store it back on the record.

    Never drops a record (always returns True) and never raises: ``redact_secrets`` is pure
    regex over a bounded ``str``, and the one call that can raise - ``getMessage()`` on a bad
    %-format - is caught and the text replaced by a fixed placeholder. ``record.args`` is set
    to None so the handler's Formatter does a plain string lookup instead of a second ``%``
    pass over text that may now contain a literal ``%``. Mutating the record in place is the
    stdlib's documented filter contract, and it is what makes the redaction hold for every
    handler the record is then offered to.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = redact_secrets(record.getMessage())
        except Exception:  # noqa: BLE001 - any failure here must neither raise nor drop
            rendered = _UNRENDERABLE_RECORD_MESSAGE
        record.msg = rendered
        record.args = None
        return True


def configure_log_hygiene() -> None:
    """Pin the third-party loggers to WARNING and add the redacting filter to every root
    handler. Idempotent; call once from ``server.main()`` after the SDK has installed its
    handler (it does so when ``MCPServer`` is constructed, at import) and before ``mcp.run()``.

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
