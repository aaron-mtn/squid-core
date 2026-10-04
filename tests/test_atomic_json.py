"""Tests for atomic writes of small JSON state files."""

import json
from pathlib import Path
from unittest.mock import call

import pytest

from squid_core.storage.atomic_json import atomic_write_json


def test_atomic_write_json_writes_existing_format(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"

    atomic_write_json(destination, {"enabled": True, "items": [1, 2]})

    assert destination.read_text(encoding="utf-8") == (
        '{\n  "enabled": true,\n  "items": [\n    1,\n    2\n  ]\n}\n'
    )
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "enabled": True,
        "items": [1, 2],
    }


def test_permission_error_on_first_replace_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "state.json"
    real_replace = __import__("os").replace
    attempts = 0

    def contend_once(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(5, "Access is denied", target)
        real_replace(source, target)

    sleeps = []
    monkeypatch.setattr("squid_core.storage.atomic_json.os.replace", contend_once)
    monkeypatch.setattr("squid_core.storage.atomic_json.time.sleep", sleeps.append)

    atomic_write_json(destination, {"status": "new"})

    assert attempts == 2
    assert sleeps == [0.02]
    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "new"}
    assert list(tmp_path.iterdir()) == [destination]


def test_several_permission_errors_are_retried_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "state.json"
    real_replace = __import__("os").replace
    attempts = 0

    def contend_three_times(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise PermissionError(5, "Access is denied", target)
        real_replace(source, target)

    sleeps = []
    monkeypatch.setattr(
        "squid_core.storage.atomic_json.os.replace", contend_three_times
    )
    monkeypatch.setattr("squid_core.storage.atomic_json.time.sleep", sleeps.append)

    atomic_write_json(destination, {"status": "new"})

    assert attempts == 4
    assert sleeps == [0.02, 0.04, 0.08]
    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "new"}
    assert list(tmp_path.iterdir()) == [destination]


def test_persistent_permission_error_propagates_and_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "state.json"
    previous = '{\n  "status": "valid"\n}\n'
    destination.write_text(previous, encoding="utf-8")
    failure = PermissionError(5, "Access is denied", destination)
    replace_calls = []
    sleeps = []

    def always_contended(source: Path, target: Path) -> None:
        replace_calls.append(call(source, target))
        raise failure

    monkeypatch.setattr("squid_core.storage.atomic_json.os.replace", always_contended)
    monkeypatch.setattr("squid_core.storage.atomic_json.time.sleep", sleeps.append)

    with pytest.raises(PermissionError) as raised:
        atomic_write_json(destination, {"status": "new"})

    assert raised.value is failure
    assert len(replace_calls) == 4
    assert sleeps == [0.02, 0.04, 0.08]
    assert destination.read_text(encoding="utf-8") == previous
    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "valid"}
    assert list(tmp_path.iterdir()) == [destination]


def test_serialization_failure_leaves_previous_file_intact(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    previous = '{\n  "status": "valid"\n}\n'
    destination.write_text(previous, encoding="utf-8")

    with pytest.raises(TypeError):
        atomic_write_json(destination, {"invalid": object()})

    assert destination.read_text(encoding="utf-8") == previous
    assert list(tmp_path.iterdir()) == [destination]


def test_replace_failure_does_not_install_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "state.json"
    previous = '{\n  "status": "valid"\n}\n'
    destination.write_text(previous, encoding="utf-8")

    replace_attempts = 0

    def fail_replace(source: Path, target: Path) -> None:
        nonlocal replace_attempts
        replace_attempts += 1
        raise OSError("replace failed")

    monkeypatch.setattr("squid_core.storage.atomic_json.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_json(destination, {"status": "new"})

    assert destination.read_text(encoding="utf-8") == previous
    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "valid"}
    assert list(tmp_path.iterdir()) == [destination]
    assert replace_attempts == 1
