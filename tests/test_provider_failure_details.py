"""Regression coverage for HTTP failures behind generic transport wrappers."""

from urllib.error import HTTPError

import pytest

from squid_core.model_adapter import model_request_error_details


@pytest.mark.parametrize("explicit_cause", [False, True])
@pytest.mark.parametrize("status, category, transient", [
    (429, "rate_limit", True), (500, "provider_server_error", True),
    (503, "provider_server_error", True), (400, "configuration", False),
    (404, "configuration", False), (401, "authentication", False),
])
def test_wrapped_urllib_http_error_details(explicit_cause, status, category, transient):
    """An outer RuntimeError must not hide HTTP evidence in cause or context."""
    original = HTTPError("http://localhost/api/chat", status, "fixture error", {}, None)
    try:
        try:
            raise original
        except HTTPError:
            if explicit_cause:
                raise RuntimeError("Ollama request failed") from original
            raise RuntimeError("Ollama request failed")
    except RuntimeError as error:
        details = model_request_error_details(error)

    assert details["error_type"] == "RuntimeError"
    assert details["error_message"] == "Ollama request failed"
    assert details["underlying_error_type"] == "HTTPError"
    assert details["underlying_error_message"] == f"HTTP Error {status}: fixture error"
    assert details["http_status"] == status
    assert details["failure_class"] == category
    assert details["_transient"] is transient
