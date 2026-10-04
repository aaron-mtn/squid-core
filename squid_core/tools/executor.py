"""Dispatch built-in tools with workspace and undo lifecycle safeguards."""

from pathlib import Path
from typing import Callable

from squid_core.image_content import SUPPORTED_IMAGE_SUFFIXES
from squid_core.platform_context import PlatformContext
from squid_core.safety.workspace_boundary import workspace_boundary_reason
from squid_core.settings import ReadFileSettings

from .builtins import TOOLS
from .builtins.apply_patch import PatchCandidateStore


BUILTIN_RUN_COMMAND = TOOLS["run_command"]["execute"]
BUILTIN_APPLY_PATCH = TOOLS["apply_patch"]["execute"]
BUILTIN_READ_FILE = TOOLS["read_file"]["execute"]


class ToolExecutor:
    """Owns tool dispatch, execution-time guards, normalized outputs, and simple tool side effects; must not own prompts, approval decisions, or provider logic."""

    # repo_root is the default working directory for run_command and the workspace
    # boundary for structured file-tool execution guards.
    # File tools still use absolute paths directly.
    def __init__(
        self,
        repo_root: Path,
        session,
        undo_store=None,
        allow_outside_workspace: bool = False,
        tools: dict | None = None,
        platform_context: PlatformContext | None = None,
        read_file_settings: ReadFileSettings | None = None,
        snapshot_asset=None,
        ensure_generated_image=None,
        supports_image_input: Callable[[], bool] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.session = session
        self.undo_store = undo_store
        self.allow_outside_workspace = allow_outside_workspace
        self.tools = TOOLS if tools is None else tools
        self.platform_context = platform_context
        self.read_file_settings = read_file_settings or ReadFileSettings()
        self.snapshot_asset = snapshot_asset
        self.ensure_generated_image = ensure_generated_image
        self.supports_image_input = supports_image_input
        self.should_cancel = should_cancel or (lambda: False)
        # The executor is constructed per agent run, which bounds candidate lifetime
        # without coupling diagnostic recovery to persisted Session history.
        self.patch_candidates = PatchCandidateStore()

    # Execute one tool action by looking up the tool and calling its execute function.
    # If the tool runs successfully, record any simple side effects in the session.
    def execute(self, action: dict) -> dict:
        tool_name = action["tool"]
        args = action.get("args", {})

        tool = self.tools.get(tool_name)
        if tool is None:
            return {
                "ok": False,
                "tool": tool_name,
                "error": f"Unknown tool: {tool_name}",
            }

        boundary_action = action
        normal_patch_args_complete = all(
            isinstance(args.get(name), str)
            for name in ("path", "old_text", "new_text")
        )
        stored_patch_candidate = (
            tool["execute"] is BUILTIN_APPLY_PATCH
            and isinstance(args.get("match_id"), str)
            and self.patch_candidates.get(args["match_id"]) is not None
        )
        candidate_selection = stored_patch_candidate or (
            tool["execute"] is BUILTIN_APPLY_PATCH
            and isinstance(args.get("match_id"), str)
            and not normal_patch_args_complete
        )
        if candidate_selection:
            # Candidate selection is pathless semantically. A redundant caller path
            # is checked against the stored target by apply_patch, not trusted here.
            boundary_action = {**action, "args": {"match_id": args["match_id"]}}

        outside_workspace_approved = bool(action.get("outside_workspace_approved"))
        if not self.allow_outside_workspace and not outside_workspace_approved:
            constraint_reason = workspace_boundary_reason(boundary_action, self.repo_root)
            if constraint_reason is not None:
                return {
                    "ok": False,
                    "tool": tool_name,
                    "error": constraint_reason,
                }

        provisional_snapshot_path = None
        try:
            if self.undo_store is not None and tool_name in {"write_file", "apply_patch"}:
                if candidate_selection:
                    # The retained candidate stays authoritative even when the model
                    # redundantly supplies a path in the selection call.
                    path = self.patch_candidates.path_for(args["match_id"])
                else:
                    path = args.get("path")
                if isinstance(path, str):
                    snapshot_already_existed = self.undo_store.has_snapshot(path)
                    self.undo_store.snapshot_file(path)
                    if not snapshot_already_existed:
                        provisional_snapshot_path = path

            execute_args = args
            if tool["execute"] is BUILTIN_RUN_COMMAND:
                execute_args = {
                    **args,
                    "platform_context": self.platform_context,
                    "should_cancel": self.should_cancel,
                }
            elif tool["execute"] is BUILTIN_APPLY_PATCH:
                execute_args = {**args, "candidate_store": self.patch_candidates}
            elif tool["execute"] is BUILTIN_READ_FILE:
                image_input_supported = None
                path = args.get("path")
                if (
                    isinstance(path, str)
                    and Path(path).suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
                    and self.supports_image_input is not None
                ):
                    image_input_supported = self.supports_image_input()
                execute_args = {
                    **args,
                    "settings": self.read_file_settings,
                    "snapshot_asset": self.snapshot_asset,
                    "ensure_generated_image": self.ensure_generated_image,
                    "image_input_supported": image_input_supported,
                }

            result = tool["execute"](self.repo_root, **execute_args)
            if not result.get("ok") and provisional_snapshot_path is not None:
                self.undo_store.discard_snapshot_if_unchanged(provisional_snapshot_path)
            self._record_tool_effects(tool_name, result)
            return result
        except Exception as e:
            if provisional_snapshot_path is not None:
                try:
                    self.undo_store.discard_snapshot_if_unchanged(provisional_snapshot_path)
                except Exception:
                    pass
            return {
                "ok": False,
                "tool": tool_name,
                "error": str(e),
            }

    # Record simple factual side effects of successful tool execution.
    # This is only for lightweight session tracking, not higher-level policy.
    def _record_tool_effects(self, tool_name: str, result: dict) -> None:
        if not result.get("ok"):
            return

        if tool_name == "read_file":
            path = result.get("path")
            if path:
                self.session.mark_file_read(path)

        if tool_name in {"write_file", "apply_patch"}:
            path = result.get("path")
            if path:
                self.patch_candidates.invalidate_path(path)
                self.session.mark_file_written(path)
