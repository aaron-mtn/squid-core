from squid_core.safety import ActionPreflight


def test_allowed_guarded_action_passes_through_unchanged(tmp_path) -> None:
    target = tmp_path / "README.md"
    action = {"tool": "read_file", "args": {"path": str(target)}}

    decision = ActionPreflight(tmp_path).check(action)

    assert decision.approval_action == action
    assert decision.execution_action == action


def test_outside_workspace_action_requires_approval_and_marks_execution(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    action = {
        "tool": "write_file",
        "args": {"path": str(tmp_path / "outside.txt"), "content": "hello"},
    }

    decision = ActionPreflight(workspace).check(action)

    assert "outside the workspace root" in decision.approval_action["approval_reason"]
    assert decision.execution_action == {
        **action,
        "outside_workspace_approved": True,
    }


def test_preflight_preserves_hook_reason_without_classifying_command(tmp_path) -> None:
    action = {"tool": "run_command", "args": {"command": "mkdir build"}}

    decision = ActionPreflight(tmp_path).check(
        action,
        additional_approval_reason="hook reason",
    )

    assert decision.approval_action["approval_reason"] == "hook reason"
    assert decision.execution_action == action
