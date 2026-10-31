"""Verify Core's bounded workspace listing, independent of model inference.

These tests own filesystem and executor contracts; application Skill exposure
is covered by Code Squid and Desktop integration tests.
"""

import importlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from squid_core.session import Session
from squid_core.tools.builtins import TOOLS
from squid_core.tools.executor import ToolExecutor


listing = importlib.import_module("squid_core.tools.builtins.list_files")


def test_lists_immediate_entries_in_stable_directory_first_order(tmp_path):
    (tmp_path / "z-dir").mkdir()
    (tmp_path / "A-dir").mkdir()
    (tmp_path / "z-dir" / "nested.txt").touch()
    for name in ("z.txt", "a.txt", "B.txt"):
        (tmp_path / name).touch()
    result = listing.execute(tmp_path)
    assert result["ok"] and not result["truncated"]
    assert result["path"] == str(tmp_path.resolve())
    assert result["entries"] == [
        {"name": name, "path": str(tmp_path / name), "type": kind}
        for name, kind in [("A-dir", "directory"), ("z-dir", "directory"),
                           ("a.txt", "file"), ("B.txt", "file"), ("z.txt", "file")]
    ]
    assert TOOLS["list_files"]["execute"] is listing.execute
    assert listing.tool_definition()["function"]["parameters"]["required"] == []


def test_empty_directory_and_absolute_subdirectory(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    result = listing.execute(tmp_path, str(child))
    assert result["ok"] and result["entries"] == []
    assert result["truncated"] is False and result["truncation_reason"] is None


@pytest.mark.parametrize("path", ["", "relative", 1, True, [], {}, "bad\0path"])
def test_invalid_arguments(tmp_path, path):
    assert listing.execute(tmp_path, path)["ok"] is False


def test_missing_path_and_file_target(tmp_path):
    assert listing.execute(tmp_path, str(tmp_path / "missing"))["error"] == "Directory does not exist"
    file = tmp_path / "file"
    file.touch()
    assert listing.execute(tmp_path, str(file))["error"] == "Requested path is not a directory"


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_access_errors_do_not_return_partial_contents(tmp_path, monkeypatch, failure):
    def fail(_path):
        raise failure("sensitive OS details")
    monkeypatch.setattr(listing.os, "scandir", fail)
    result = listing.execute(tmp_path)
    assert not result["ok"] and "entries" not in result
    assert "sensitive" not in json.dumps(result)


def test_outside_and_parent_traversal_are_denied_even_with_executor_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret").touch()
    executor = ToolExecutor(workspace, Session(workspace), allow_outside_workspace=True)
    for target in (str(tmp_path), str(workspace / "..")):
        result = executor.execute({"tool": "list_files", "args": {"path": target},
                                   "outside_workspace_approved": True})
        assert not result["ok"] and "outside" in result["error"]
        assert "secret" not in json.dumps(result)


def test_external_directory_link_and_linked_ancestor_are_denied(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "nested").mkdir()
    (outside / "secret").touch()
    link = workspace / "link"
    if os.name == "nt":
        # Junctions need no symlink privilege and exercise Windows reparse points.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    result = listing.execute(workspace)
    assert result["entries"] == [{"name": "link", "path": str(link), "type": "link"}]
    for target in (link, link / "nested"):
        assert not listing.execute(workspace, str(target))["ok"]
    assert "secret" not in json.dumps(result)


def test_entry_limit_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(listing, "MAX_ENTRIES", 3)
    for name in ("z", "d", "c", "a", "b"):
        (tmp_path / name).touch()
    result = listing.execute(tmp_path)
    assert [entry["name"] for entry in result["entries"]] == ["a", "b", "c"]
    assert result["truncated"] and result["truncation_reason"] == "entry_limit"


def test_complete_json_response_obeys_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(listing, "MAX_RESPONSE_BYTES", 1500)
    for index in range(20):
        (tmp_path / (f"{index:02}-" + "é" * 60)).touch()
    result = listing.execute(tmp_path)
    assert result["ok"] and result["entries"]
    assert len(json.dumps(result).encode("utf-8")) <= listing.MAX_RESPONSE_BYTES
    assert result["truncated"] and result["truncation_reason"] == "response_size_limit"
