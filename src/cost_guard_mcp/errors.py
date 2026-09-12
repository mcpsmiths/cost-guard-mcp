import functools
import re
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

_SECRET_PATTERNS = [
    re.compile(r'(password|passwd|pwd)["\']?\s*[=:]\s*["\']?[^\s,"\'}]+', re.IGNORECASE),
    re.compile(r"(private[_-]?key)\s*[=:]\s*[^,}]*?(?=\s*(?:[,}]|$))", re.IGNORECASE),
    re.compile(r"(token|api[_-]?key|secret)\s*[=:]\s*[^\s,}]+", re.IGNORECASE),
]


class SanitizedEngineError(RuntimeError):
    """Raised in place of a raw warehouse-client exception. Safe to log or return to a tool caller."""


def _redact(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
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
            except Exception as exc:
                safe_message = _redact(str(exc))
                raise SanitizedEngineError(f"{engine} client call failed: {safe_message}") from None

        return wrapper

    return decorator
