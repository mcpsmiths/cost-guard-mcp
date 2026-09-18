import functools
import inspect
import re
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar, cast

from mcp.server.mcpserver.exceptions import ToolError

from cost_guard_mcp.config import ConfigError

P = ParamSpec("P")
T = TypeVar("T")

# Shared value-matching fragment for the password/token patterns below. A bare (unquoted)
# value is allowed to contain internal single spaces (real passwords legitimately can -
# e.g. SNOWFLAKE_PASSWORD is an arbitrary user-chosen string) via the repetition's
# lookahead, but that lookahead stops the moment what follows looks like the start of the
# *next* key=value/key:value pair, so a multi-word value doesn't swallow an unrelated
# trailing field. A quoted value (single or double) is matched up to its closing quote
# regardless of what it contains, so both quotes are consumed and never leak.
_BARE_WORD = r'[^\s,;"\'}=:]+'
_VALUE_FRAGMENT = (
    r'"[^"]*"' r"|'[^']*'" rf"|{_BARE_WORD}(?:\s+(?!{_BARE_WORD}\s*[=:]){_BARE_WORD})*"
)

_SECRET_PATTERNS = [
    # Password in various formats: password=value, 'password': value, "password": value.
    # Deliberately no leading ["\']? before the value fragment - the quoted alternatives
    # inside _VALUE_FRAGMENT consume their own opening AND closing quote.
    re.compile(rf'(password|passwd|pwd)["\']?\s*[=:]\s*(?:{_VALUE_FRAGMENT})', re.IGNORECASE),
    # Private key (with optional passphrase) - handles PEM blocks and quoted multiline values
    # Matches: private_key=-----BEGIN...-----END... or private_key='value' or 'private_key': 'value'
    re.compile(
        r'(private[_-]?key(?:[_-]?passphrase)?)["\']?\s*[=:]\s*'
        r'(?:-----BEGIN[^\n]*(?:\n[^\n]*)*?\n-----END[^\n]*-----|["\'](?:[^"\']*)["\']|(?:[^\s,}]|\s(?![\s,}]))+)',
        re.IGNORECASE | re.DOTALL,
    ),
    # Token, API key, secret - same whitespace-tolerant/quote-aware value fragment as password.
    re.compile(rf'(token|api[_-]?key|secret)["\']?\s*[=:]\s*(?:{_VALUE_FRAGMENT})', re.IGNORECASE),
    # Standalone PEM block (full multiline from BEGIN to END)
    re.compile(r"-----BEGIN[^\n]*-----.*?-----END[^\n]*-----", re.DOTALL | re.IGNORECASE),
    # Snowflake connection URI format: user:password@host
    re.compile(r"(://[^:/@]+:)([^\s/@]+)(?=@)", re.IGNORECASE),
]


class SanitizedEngineError(RuntimeError):
    """Raised in place of a raw warehouse-client exception. Safe to log or return to a tool caller."""


class UserVisibleError(ValueError):
    """A ValueError this codebase has deliberately vetted as containing no secrets or
    internal detail. Raise this instead of a bare ValueError when the message should reach
    the calling agent via as_tool_error below — a plain ValueError from somewhere
    unaudited (a library call, a future code path) is NOT assumed safe and will NOT be
    forwarded to the client.
    """


def redact_secrets(text: str) -> str:
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
    return redacted


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
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except (SanitizedEngineError, ConfigError, UserVisibleError) as exc:
            raise ToolError(str(exc)) from exc

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
                safe_message = redact_secrets(str(exc))
                raise SanitizedEngineError(f"{engine} client call failed: {safe_message}") from None

        return wrapper

    return decorator
