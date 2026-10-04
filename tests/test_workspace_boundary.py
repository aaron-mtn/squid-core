from pathlib import Path

from squid_core.safety.workspace_boundary import workspace_boundary_reason


def test_absolute_file_path_inside_workspace_allowed(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"

    assert (
        workspace_boundary_reason(
            {"tool": "read_file", "args": {"path": str(target)}},
            tmp_path,
        )
        is None
    )


def test_absolute_file_path_outside_workspace_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()

    reason = workspace_boundary_reason(
        {"tool": "write_file", "args": {"path": str(outside)}},
        workspace,
    )

    assert reason is not None
    assert str(outside) in reason
    assert str(workspace) in reason
    assert "outside the workspace root" in reason
    assert "Approval is required unless approval_mode=never" in reason


def test_relative_file_path_left_to_tool_validation(tmp_path: Path) -> None:
    assert (
        workspace_boundary_reason(
            {"tool": "read_file", "args": {"path": "file.txt"}},
            tmp_path,
        )
        is None
    )


def test_run_command_plain_command_allowed(tmp_path: Path) -> None:
    assert (
        workspace_boundary_reason(
            {"tool": "run_command", "args": {"command": "cat /etc/hosts"}},
            tmp_path,
        )
        is None
    )
