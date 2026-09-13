import functools
import re
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from cost_guard_mcp.config import ConfigError

P = ParamSpec("P")
T = TypeVar("T")

_SECRET_PATTERNS = [
    # Password in various formats: password=value, 'password': value, "password": value
    re.compile(r'(password|passwd|pwd)["\']?\s*[=:]\s*["\']?[^\s,"\'}]+', re.IGNORECASE),
    # Private key (with optional passphrase) - handles PEM blocks and quoted multiline values
    # Matches: private_key=-----BEGIN...-----END... or private_key='value' or 'private_key': 'value'
    re.compile(
        r'(private[_-]?key(?:[_-]?passphrase)?)["\']?\s*[=:]\s*'
        r'(?:-----BEGIN[^\n]*(?:\n[^\n]*)*?\n-----END[^\n]*-----|["\'](?:[^"\']*)["\']|(?:[^\s,}]|\s(?![\s,}]))+)',
        re.IGNORECASE | re.DOTALL,
    ),
    # Token, API key, secret - with optional quotes before separator
    re.compile(r'(token|api[_-]?key|secret)["\']?\s*[=:]\s*["\']?[^\s,"\'}]+', re.IGNORECASE),
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
    """

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
