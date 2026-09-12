import base64
import pytest

from cost_guard_mcp.errors import SanitizedEngineError, sanitize_exceptions


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
