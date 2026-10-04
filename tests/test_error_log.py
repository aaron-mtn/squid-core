from squid_core.storage import write_error_log


def test_write_error_log_writes_latest_log_with_exception_text(tmp_path) -> None:
    error = RuntimeError("provider unavailable")

    expected_path = tmp_path / ".code-squid" / "logs" / "latest.log"
    log_path = write_error_log(expected_path, error)
    log_text = log_path.read_text(encoding="utf-8")

    assert log_path == expected_path
    assert log_text.startswith("Timestamp: ")
    assert "RuntimeError: provider unavailable" in log_text
