"""Small preflight checks for structured file tools that target outside the workspace."""

from __future__ import annotations

from pathlib import Path


FILE_TOOLS = {"read_file", "write_file", "apply_patch"}


def is_inside_workspace(path: Path, workspace_root: Path) -> bool:
    resolved_path = path.expanduser().resolve()
    resolved_root = workspace_root.expanduser().resolve()

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False

    return True


def workspace_boundary_reason(action: dict, workspace_root: Path) -> str | None:
    tool_name = action.get("tool")
    args = action.get("args", {})
    if not isinstance(args, dict):
        return None

    if tool_name in FILE_TOOLS:
        path = args.get("path")
        if not isinstance(path, str):
            return None
        return _file_path_reason(path, workspace_root)

    return None


def _file_path_reason(path: str, workspace_root: Path) -> str | None:
    file_path = Path(path)
    if not file_path.is_absolute():
        return None

    if is_inside_workspace(file_path, workspace_root):
        return None

    return _outside_workspace_message(path, workspace_root)


def _outside_workspace_message(target: str, workspace_root: Path) -> str:
    return (
        f"Requested path is outside the workspace root: {target} "
        f"(workspace root: {workspace_root}). Approval is required unless approval_mode=never."
    )
