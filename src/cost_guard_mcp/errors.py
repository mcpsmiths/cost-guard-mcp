import functools
import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar, cast

from mcp.server.mcpserver.exceptions import ToolError

from cost_guard_mcp.config import ConfigError

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)

# Upper bound on the text redact_secrets scans. Exception text is model- or warehouse-
# controlled and otherwise unbounded (neither src/ nor the SDK's stdio transport caps an
# argument's size), and every pattern below runs over all of it inside the single stdio
# process with the GIL held - so the scan is capped here, before any pattern runs, and the
# dropped tail is replaced by a marker. Real connector errors are never this long.
_MAX_REDACTED_TEXT_CHARS = 16_384

# Shared value-matching fragment for the password/token patterns below. A bare (unquoted)
# value is allowed to contain internal single spaces (real passwords legitimately can -
# e.g. SNOWFLAKE_PASSWORD is an arbitrary user-chosen string) via the repetition's
# lookahead, but that lookahead stops the moment what follows looks like the start of the
# *next* key=value/key:value pair, so a multi-word value doesn't swallow an unrelated
# trailing field. A quoted value (single or double) is matched up to its closing quote
# regardless of what it contains, so both quotes are consumed and never leak - or, when the
# closing quote never comes (the bound above can cut inside a quoted value), to end of text.
_BARE_WORD = r'[^\s,;"\'}=:]+'
_DOUBLE_QUOTED = r'"[^"]*(?:"|\Z)'
_SINGLE_QUOTED = r"'[^']*(?:'|\Z)"
_VALUE_FRAGMENT = (
    rf"{_DOUBLE_QUOTED}|{_SINGLE_QUOTED}"
    rf"|{_BARE_WORD}(?:\s+(?!{_BARE_WORD}\s*[=:]){_BARE_WORD})*"
)

# Every pattern here must match in time linear in the text: they run on the error path of
# the single-process server, over text an attacker can shape, and CPython's regex engine
# holds the GIL for the whole match. Each quantified class below is unambiguous about where
# it stops, and nothing lazy is left without a terminating alternative.
_SECRET_PATTERNS = [
    # Standalone PEM block, first so the key=value patterns below only ever see its
    # placeholder. The label class excludes '-', so the header matches exactly one way, and
    # a BEGIN with no END swallows to end of text once (\Z) rather than rescanning the tail
    # for every candidate ending - the previous `[^\n]*-----.*?-----END` form backtracked
    # quadratically on a dash-run header (~4 s per 32 KB, x4 per doubling).
    re.compile(r"-----BEGIN[^\n-]*-----.*?(?:-----END[^\n]*-----|\Z)", re.DOTALL | re.IGNORECASE),
    # Password in various formats: password=value, 'password': value, "password": value.
    # Deliberately no leading ["\']? before the value fragment - the quoted alternatives
    # inside _VALUE_FRAGMENT consume their own opening AND closing quote.
    re.compile(rf'(password|passwd|pwd)["\']?\s*[=:]\s*(?:{_VALUE_FRAGMENT})', re.IGNORECASE),
    # Private key (with optional passphrase): private_key='value', 'private_key': 'value', or
    # a bare value that may contain single internal spaces. A PEM-block value has already
    # been reduced to the placeholder by the pattern above, so there is no PEM alternative
    # here (the old one rescanned to end of text once per BEGIN without an END).
    re.compile(
        r'(private[_-]?key(?:[_-]?passphrase)?)["\']?\s*[=:]\s*'
        rf"(?:{_DOUBLE_QUOTED}|{_SINGLE_QUOTED}|(?:[^\s,}}]|\s(?![\s,}}]))+)",
        re.IGNORECASE,
    ),
    # Token, API key, secret - same whitespace-tolerant/quote-aware value fragment as password.
    re.compile(rf'(token|api[_-]?key|secret)["\']?\s*[=:]\s*(?:{_VALUE_FRAGMENT})', re.IGNORECASE),
    # Snowflake connection URI format: user:password@host
    re.compile(r"(://[^:/@]+:)([^\s/@]+)(?=@)", re.IGNORECASE),
]


class SanitizedEngineError(RuntimeError):
    """Raised in place of a raw warehouse-client exception. Secret-free and one line: the
    quoted text it carries is whatever the failing call raised, escaped by
    present_remote_text, not server guidance."""


class UserVisibleError(ValueError):
    """A ValueError this codebase has deliberately vetted as containing no secrets or
    internal detail. Raise this instead of a bare ValueError when the message should reach
    the calling agent via as_tool_error below — a plain ValueError from somewhere
    unaudited (a library call, a future code path) is NOT assumed safe and will NOT be
    forwarded to the client.
    """


def redact_secrets(text: str) -> str:
    """Substitute every secret-shaped fragment of `text` with a placeholder.

    Scans at most _MAX_REDACTED_TEXT_CHARS characters: the tail beyond the bound is dropped
    before any pattern runs and replaced by a ' ...[truncated N chars]' marker appended after
    redaction, so the marker itself is never consumed by a pattern. Together with the linear
    patterns above this keeps the error path constant-overhead regardless of what the model
    or the warehouse put in the exception text.
    """
    marker = ""
    if len(text) > _MAX_REDACTED_TEXT_CHARS:
        marker = f" ...[truncated {len(text) - _MAX_REDACTED_TEXT_CHARS} chars]"
        text = text[:_MAX_REDACTED_TEXT_CHARS]
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith(r"(://"):  # URI pattern
            redacted = pattern.sub(r"\1***REDACTED***", redacted)
        elif pattern.pattern.startswith("-----BEGIN"):  # PEM block pattern (no groups)
            placeholder = (
                "-----"
                + "BEGIN PRIVATE KEY"
                + "----- ***REDACTED*** -----"
                + "END PRIVATE KEY"
                + "-----"
            )
            redacted = pattern.sub(placeholder, redacted)
        else:
            redacted = pattern.sub(lambda m: f"{m.group(1)}=***REDACTED***", redacted)
    return redacted + marker


def present_remote_text(text: str) -> str:
    """Quote text that did not originate in this process before it reaches a log record or a
    tool caller.

    repr() escapes every control and other non-printable character - C0 (\\n, \\r, ESC, NUL),
    DEL, C1 (NEL), the Unicode line/paragraph separators and bidi controls - as \\xNN /
    \\uNNNN, so the result is exactly one line that still shows the original content for
    forensics, and the surrounding quotes make it visible where the untrusted text starts
    and ends. Runs AFTER redact_secrets, never before: redaction has to see the raw text
    (a multi-line PEM pattern needs real newlines), and nothing that can introduce a line
    boundary must be written to stderr or returned to the model un-escaped - the SDK's own
    stderr handler is a bare '%(message)s', so an embedded newline would otherwise start a
    new, prefix-free record indistinguishable from a genuine one.
    """
    return repr(text)


def _log_tool_outcome(tool_name: str, start_monotonic: float, outcome: str) -> None:
    """Log a single, secrets-free record for one as_tool_error-wrapped tool call.

    Never receives (or logs) the exception message itself - only the tool name, a
    coarse outcome label, and elapsed wall time - so this can't leak whatever a future
    unaudited exception's text might contain.
    """
    elapsed_ms = (time.monotonic() - start_monotonic) * 1000
    logger.info("tool=%s outcome=%s elapsed_ms=%.1f", tool_name, outcome, elapsed_ms)


def as_tool_error[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    """Wrap an @mcp.tool()-decorated function so its known, already-safe exceptions reach
    the MCP client's model instead of being silently discarded.

    The mcp SDK only passes an exception's message to the client when it is (or wraps) a
    `ToolError`; any other exception type becomes an `UnexpectedToolError`, whose message
    is deliberately the generic "Error executing tool <name>" — the original text "stays on
    the server" by design (see mcp.server.mcpserver.tools.base). That default is the right
    call for exceptions we didn't anticipate, but it also silently swallows the messages
    this project already goes out of its way to make safe: `SanitizedEngineError` (redacted
    by sanitize_exceptions above), `ConfigError` (never contains secrets), and
    `UserVisibleError` (a ValueError this codebase deliberately vetted as safe — see its own
    docstring). Converting exactly those three into `ToolError` is what lets an agent
    actually see (and act on) the hint/reason text instead of a black box. Deliberately NOT
    catching the bare `ValueError` type: that would forward the message of any ValueError
    from anywhere in the call chain — including a future, unaudited one — to the client.

    Supports both sync and async `func`. An async `func` gets an async wrapper back, so the
    mcp SDK's own `is_async_callable` check (see `mcp.server.mcpserver.tools.base.Tool`)
    still sees a coroutine function and awaits it directly on the event loop, instead of
    silently handing a sync-looking wrapper to the SDK's implicit-sync-wrapping thread pool -
    which would both defeat the point of an async tool body and never actually run it (a sync
    wrapper calling an async `func` would just produce an unawaited coroutine object).
    """

    if inspect.iscoroutinefunction(func):
        async_func = cast(Callable[P, Awaitable[T]], func)

        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return await async_func(*args, **kwargs)
            except ToolError:
                raise
            except (SanitizedEngineError, ConfigError, UserVisibleError) as exc:
                raise ToolError(str(exc)) from exc

        return cast(Callable[P, T], async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        tool_name = func.__name__
        start = time.monotonic()
        try:
            result = func(*args, **kwargs)
        except ToolError:
            _log_tool_outcome(tool_name, start, "error")
            raise
        except (SanitizedEngineError, ConfigError, UserVisibleError) as exc:
            _log_tool_outcome(tool_name, start, "error")
            raise ToolError(str(exc)) from exc
        except Exception:
            # Bare re-raise below - ruff's BLE001 (blind-except) deliberately exempts this
            # pattern, since the exception itself is untouched and propagates exactly as it
            # would without this decorator; this branch exists purely to log the outcome for
            # every previously-uncaught exception type too.
            _log_tool_outcome(tool_name, start, "error")
            raise
        else:
            _log_tool_outcome(tool_name, start, "success")
            return result

    return wrapper


def sanitize_exceptions(engine: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Wrap a warehouse-client call so no raw exception (which may embed credentials) escapes."""

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except SanitizedEngineError:
                raise
            except Exception as exc:  # noqa: BLE001 - intentionally catching all exceptions to sanitize
                presented = present_remote_text(redact_secrets(str(exc)))
                logger.warning(
                    "engine=%s warehouse_client_call_failed message=%s", engine, presented
                )
                raise SanitizedEngineError(f"{engine} client call failed: {presented}") from None

        return wrapper

    return decorator
