import functools
import re
from collections.abc import Callable
from typing import ParamSpec, TypeVar

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


def _redact(text: str) -> str:
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
                safe_message = _redact(str(exc))
                raise SanitizedEngineError(f"{engine} client call failed: {safe_message}") from None

        return wrapper

    return decorator
