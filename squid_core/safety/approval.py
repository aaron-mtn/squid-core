"""Small UI-agnostic approval policy for tool actions."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol


APPROVAL_NEVER = "never"
APPROVAL_ALWAYS = "always"
APPROVAL_GUARDED = "guarded"

USER_DENIED_TOOL_ACTION = "user denied tool action"


class ApprovalPrompter(Protocol):
    def approve_action(self, action: dict) -> "ApprovalDecision":
        """Return the user's approval decision for a tool action."""


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    instruction: str = ""
    reason: str = ""
    always_approve_command: bool = False


_COMMAND_IDENTITY_PATTERN = re.compile(r"^\s*(?:\"([^\"]+)\"|'([^']+)'|(\S+))")
_POWERSHELL_CALL_OPERATOR_PATTERN = re.compile(r"^\s*&(?=\s|$)")
_POWERSHELL_LITERAL_IDENTITY_PATTERN = re.compile(
    r'^(?:"([^"$`]+)"|\'([^\']+)\'|([^\s$`"\';&|<>(){}\[\],]+))(?=\s|$)'
)
_POWERSHELL_DYNAMIC_CALL_PREFIXES = ("$", "(", "{", "[", "@")


def command_identity(command: str) -> str | None:
    """Return the normalized literal program token without parsing the shell.

    PowerShell's leading call operator is skipped only when followed by a
    straightforward literal token. Dynamic call expressions deliberately have
    no trustable identity rather than risking persistent trust in ``&`` or in
    a misleading fragment of an expression.
    """

    call_operator = _POWERSHELL_CALL_OPERATOR_PATTERN.match(command)
    if call_operator is not None:
        remainder = command[call_operator.end() :].lstrip(" \t")
        match = _POWERSHELL_LITERAL_IDENTITY_PATTERN.match(remainder)
        if match is None:
            return None
        double_quoted, single_quoted, bare = match.groups()
        # These prefixes introduce variables, expressions, script blocks,
        # type expressions, or splatting instead of a literal program name.
        if bare is not None and bare.startswith(_POWERSHELL_DYNAMIC_CALL_PREFIXES):
            return None
        return next(
            value for value in (double_quoted, single_quoted, bare) if value is not None
        ).casefold()

    match = _COMMAND_IDENTITY_PATTERN.match(command)
    if match is None:
        return None
    return next(value for value in match.groups() if value is not None).casefold()


class ApprovalPolicy:
    """Owns approval-mode decisions without depending on a UI implementation."""

    def __init__(
        self,
        mode: str,
        prompter: ApprovalPrompter | None = None,
        *,
        trusted_commands: Iterable[str] = (),
        persist_trusted_command: Callable[[str], None] | None = None,
    ) -> None:
        if mode not in {APPROVAL_NEVER, APPROVAL_ALWAYS, APPROVAL_GUARDED}:
            raise ValueError(f"Unknown approval mode: {mode}")

        self.mode = mode
        self.prompter = prompter
        self.trusted_commands = {
            identity.casefold() for identity in trusted_commands if identity
        }
        self.persist_trusted_command = persist_trusted_command

    def check(self, action: dict) -> ApprovalDecision:
        if self.mode == APPROVAL_NEVER:
            return ApprovalDecision(approved=True)

        command = None
        if self.mode == APPROVAL_GUARDED:
            is_run_command = action.get("tool") == "run_command"
            args = action.get("args", {})
            raw_command = args.get("command") if isinstance(args, dict) else None
            identity = (
                command_identity(raw_command)
                if is_run_command and isinstance(raw_command, str)
                else None
            )
            is_trusted = identity is not None and identity in self.trusted_commands

            if action.get("approval_reason"):
                # Command trust never overrides a separate workspace or Hook
                # reason, but an untrusted command can still be remembered.
                command = identity if not is_trusted else None
            elif not is_run_command:
                return ApprovalDecision(approved=True)
            elif is_trusted:
                return ApprovalDecision(approved=True)
            else:
                command = identity
                action = {
                    **action,
                    "approval_reason": (
                        f"{command} commands are not trusted"
                        if command is not None
                        else "Command identity is missing"
                    ),
                }

            if command is not None:
                # These UI hints describe the decision available for this one
                # prompt; they are never forwarded to tool execution.
                action = {
                    **action,
                    "command_identity": command,
                    "allow_persistent_command_approval": True,
                }

        if self.prompter is None:
            return ApprovalDecision(
                approved=False,
                reason="approval prompter required",
            )

        decision = self.prompter.approve_action(action)
        if isinstance(decision, bool):
            decision = ApprovalDecision(approved=decision)

        if decision.approved:
            if decision.always_approve_command and command is not None:
                # Update memory only after persistence succeeds, so a failed
                # settings write cannot appear to have saved the user's choice.
                if self.persist_trusted_command is not None:
                    self.persist_trusted_command(command)
                self.trusted_commands.add(command)
            return ApprovalDecision(approved=True)

        return ApprovalDecision(
            approved=False,
            instruction=decision.instruction,
            reason=decision.reason or USER_DENIED_TOOL_ACTION,
        )


def denied_result(action: dict, reason: str) -> dict:
    return {
        "ok": False,
        "tool": action.get("tool", ""),
        "error": reason,
    }
