"""Preflight policy for accepted tool actions."""

from __future__ import annotations

from dataclasses import dataclass

from .workspace_boundary import workspace_boundary_reason


@dataclass(frozen=True)
class ActionPreflightDecision:
    """The action forms needed by approval and execution."""

    approval_action: dict
    execution_action: dict


class ActionPreflight:
    """Own workspace-boundary and hook-provided approval reasons."""

    def __init__(self, workspace_root) -> None:
        self.workspace_root = workspace_root

    @classmethod
    def from_runtime(cls, tool_executor, _approval_policy) -> "ActionPreflight":
        return cls(
            workspace_root=getattr(tool_executor, "repo_root", None),
        )

    def check(
        self,
        action: dict,
        *,
        additional_approval_reason: str = "",
    ) -> ActionPreflightDecision:
        workspace_reason = self._workspace_reason(action)
        approval_reason = "; ".join(
            reason
            for reason in (
                additional_approval_reason,
                workspace_reason,
            )
            if reason
        ) or None

        approval_action = (
            {**action, "approval_reason": approval_reason}
            if approval_reason is not None
            else action
        )
        execution_action = (
            {**action, "outside_workspace_approved": True}
            if workspace_reason is not None
            else action
        )
        return ActionPreflightDecision(
            approval_action=approval_action,
            execution_action=execution_action,
        )

    def _workspace_reason(self, action: dict) -> str | None:
        if self.workspace_root is None:
            return None
        return workspace_boundary_reason(action, self.workspace_root)
