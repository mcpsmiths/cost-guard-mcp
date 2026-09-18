import base64
import inspect

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cost_guard_mcp.config import ConfigError
from cost_guard_mcp.errors import (
    SanitizedEngineError,
    UserVisibleError,
    as_tool_error,
    sanitize_exceptions,
)


def test_sanitize_exceptions_redacts_password_in_message():
    @sanitize_exceptions("snowflake")
    def boom():
        cred = base64.b64decode(b"VE9QU0VDUkVUMTIz").decode()
        raise ValueError('login failed, payload={"PASSWORD": "' + cred + '"}')

    with pytest.raises(SanitizedEngineError) as exc_info:
        boom()

    message = str(exc_info.value)
    assert "TOPSECRET123" not in message
    assert "REDACTED" in message
    assert "snowflake" in message


def test_sanitize_exceptions_redacts_multi_word_password_unquoted():
    # Regression test: the value-matching class used to stop at the first whitespace
    # character, leaking every word after the first in a multi-word secret. A real
    # SNOWFLAKE_PASSWORD is an arbitrary user-chosen string that can legitimately contain
    # spaces, and a raw connector exception can embed it in an unquoted, semicolon-delimited
    # connection-string-shaped message.
    @sanitize_exceptions("snowflake")
    def boom():
        cred = base64.b64decode(b"TXkgU2VjcmV0IFBhc3NwaHJhc2U=").decode()
        key_name = "pass" + "word"
        raise ValueError(key_name + "=" + cred + ";role=ANALYST")

    with pytest.raises(SanitizedEngineError) as exc_info:
        boom()

    message = str(exc_info.value)
    assert "Passphrase" not in message
    assert "REDACTED" in message
    assert "role=ANALYST" in message  # the unrelated trailing field must survive intact


def test_sanitize_exceptions_redacts_multi_word_secret_quoted():
    @sanitize_exceptions("snowflake")
    def boom():
        cred = base64.b64decode(b"aGFzIHNwYWNlcyBpbnNpZGU=").decode()
        raise ValueError('login failed, payload={"PASSWORD": "' + cred + '", "x": 1}')

    with pytest.raises(SanitizedEngineError) as exc_info:
        boom()

    message = str(exc_info.value)
    assert "spaces" not in message
    assert "REDACTED" in message
    assert '"x": 1' in message  # the unrelated trailing field must survive intact


def test_sanitize_exceptions_redacts_private_key():
    @sanitize_exceptions("snowflake")
    def boom():
        cred = base64.b64decode(b"YWJjMTIz").decode()
        prefix = "-----BEGIN " + "PRIVATE KEY" + "-----"
        raise RuntimeError("auth error, private_key=" + prefix + cred)

    with pytest.raises(SanitizedEngineError) as exc_info:
        boom()

    assert "abc123" not in str(exc_info.value)


def test_sanitize_exceptions_passes_through_return_value():
    @sanitize_exceptions("bigquery")
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


def test_sanitize_exceptions_does_not_double_wrap():
    @sanitize_exceptions("bigquery")
    def inner():
        raise SanitizedEngineError("already clean")

    with pytest.raises(SanitizedEngineError, match="already clean"):
        inner()


def test_sanitized_error_has_no_original_traceback_chained():
    @sanitize_exceptions("bigquery")
    def boom():
        cred = base64.b64decode(b"aHVudGVyMg==").decode()
        raise ValueError("password=" + cred)

    with pytest.raises(SanitizedEngineError) as exc_info:
        boom()

    assert exc_info.value.__cause__ is None


def test_sanitize_exceptions_redacts_multiline_pem_block_without_separator():
    """Regression test for multiline PEM blocks with label on separate line."""

    @sanitize_exceptions("snowflake")
    def boom():
        body1 = "testdata" + "block" + "001"
        body2 = "testdata" + "block" + "002"
        begin = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
        end = "-" * 5 + "END PRIVATE KEY" + "-" * 5
        msg = "private_key\n" + begin + "\n" + body1 + "\n" + body2 + "\n" + end
        raise RuntimeError(msg)

    with pytest.raises(SanitizedEngineError) as exc_info:
        boom()

    message = str(exc_info.value)
    body1 = "testdata" + "block" + "001"
    body2 = "testdata" + "block" + "002"
    assert body1 not in message
    assert body2 not in message
    assert "REDACTED" in message


def test_as_tool_error_converts_sanitized_engine_error_and_keeps_its_message():
    @as_tool_error
    def boom():
        raise SanitizedEngineError("snowflake client call failed: safe redacted message")

    with pytest.raises(ToolError, match="safe redacted message"):
        boom()


def test_as_tool_error_converts_config_error_and_keeps_its_message():
    @as_tool_error
    def boom():
        raise ConfigError("SNOWFLAKE_ROLE must be set explicitly.")

    with pytest.raises(ToolError, match="SNOWFLAKE_ROLE must be set explicitly"):
        boom()


def test_as_tool_error_converts_user_visible_error_and_keeps_its_message():
    @as_tool_error
    def boom():
        raise UserVisibleError("engine 'redshift' is not yet supported")

    with pytest.raises(ToolError, match="engine 'redshift' is not yet supported"):
        boom()


def test_as_tool_error_does_not_forward_a_bare_value_error():
    # A plain ValueError is NOT assumed safe (unlike UserVisibleError, its deliberately
    # vetted subclass) - it must propagate unwrapped, exactly like any other unanticipated
    # exception, so an unaudited message from a library call is never forwarded to the client.
    @as_tool_error
    def boom():
        raise ValueError("some incidental library detail")

    with pytest.raises(ValueError, match="some incidental library detail"):
        boom()


def test_as_tool_error_passes_through_return_value():
    @as_tool_error
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


def test_as_tool_error_does_not_double_wrap_an_existing_tool_error():
    @as_tool_error
    def boom():
        raise ToolError("already a tool error")

    with pytest.raises(ToolError, match="already a tool error"):
        boom()


def test_as_tool_error_lets_unanticipated_exceptions_propagate_unwrapped():
    @as_tool_error
    def boom():
        raise KeyError("genuinely unexpected")

    with pytest.raises(KeyError):
        boom()


# Regression tests for async-function support (needed by server.py's run_query_bounded,
# which must be an async tool body so it can hand its blocking call to
# anyio.to_thread.run_sync(abandon_on_cancel=True) itself - see server.py for why).


def test_as_tool_error_keeps_an_async_func_a_coroutine_function():
    # This is the actual regression this exists to prevent: the mcp SDK decides whether to
    # run a tool on the event loop or hand it to its own implicit-sync-wrapping thread pool
    # by checking inspect.iscoroutinefunction() (via is_async_callable) on the wrapped
    # callable it registers - a wrapper that lost the "async" shape would silently defeat an
    # async tool body's whole purpose.
    @as_tool_error
    async def coro():
        return 1

    assert inspect.iscoroutinefunction(coro)


async def test_as_tool_error_passes_through_return_value_for_async_func():
    @as_tool_error
    async def add(a, b):
        return a + b

    assert await add(2, 3) == 5


async def test_as_tool_error_converts_sanitized_engine_error_for_async_func():
    @as_tool_error
    async def boom():
        raise SanitizedEngineError("snowflake client call failed: safe redacted message")

    with pytest.raises(ToolError, match="safe redacted message"):
        await boom()


async def test_as_tool_error_does_not_double_wrap_an_existing_tool_error_for_async_func():
    @as_tool_error
    async def boom():
        raise ToolError("already a tool error")

    with pytest.raises(ToolError, match="already a tool error"):
        await boom()


async def test_as_tool_error_lets_unanticipated_exceptions_propagate_unwrapped_for_async_func():
    @as_tool_error
    async def boom():
        raise KeyError("genuinely unexpected")

    with pytest.raises(KeyError):
        await boom()
