import pytest

from squid_core.safety import (
    ApprovalDecision,
    ApprovalPolicy,
    USER_DENIED_TOOL_ACTION,
    command_identity,
)


class FakePrompter:
    def __init__(self, decision: ApprovalDecision) -> None:
        self.decision = decision
        self.actions = []

    def approve_action(self, action: dict) -> ApprovalDecision:
        self.actions.append(action)
        return self.decision


def test_approval_policy_never_approves_without_prompter() -> None:
    decision = ApprovalPolicy("never").check({"tool": "read_file"})

    assert decision.approved is True
    assert decision.reason == ""


def test_approval_policy_guarded_allows_action_without_reason() -> None:
    prompter = FakePrompter(ApprovalDecision(approved=False))

    decision = ApprovalPolicy("guarded", prompter=prompter).check({"tool": "read_file"})

    assert decision.approved is True
    assert prompter.actions == []


def test_approval_policy_guarded_prompts_for_action_with_reason() -> None:
    action = {"tool": "run_command", "approval_reason": "Command is risky"}
    prompter = FakePrompter(ApprovalDecision(approved=False))

    decision = ApprovalPolicy("guarded", prompter=prompter).check(action)

    assert decision.approved is False
    assert decision.reason == USER_DENIED_TOOL_ACTION
    assert prompter.actions == [action]


def test_guarded_untrusted_command_requires_approval() -> None:
    prompter = FakePrompter(ApprovalDecision(approved=False))

    decision = ApprovalPolicy("guarded", prompter=prompter).check(
        {"tool": "run_command", "args": {"command": "git status"}}
    )

    assert decision.approved is False
    assert prompter.actions[0]["command_identity"] == "git"
    assert prompter.actions[0]["allow_persistent_command_approval"] is True


def test_guarded_approve_once_does_not_trust_later_invocation() -> None:
    prompter = FakePrompter(ApprovalDecision(approved=True))
    policy = ApprovalPolicy("guarded", prompter=prompter)
    action = {"tool": "run_command", "args": {"command": "git status"}}

    assert policy.check(action).approved is True
    assert policy.check(action).approved is True

    assert len(prompter.actions) == 2


def test_separate_approval_reason_still_offers_command_trust() -> None:
    prompter = FakePrompter(ApprovalDecision(approved=False))

    ApprovalPolicy("guarded", prompter=prompter).check(
        {
            "tool": "run_command",
            "args": {"command": "git status"},
            "approval_reason": "hook requires confirmation",
        }
    )

    assert prompter.actions[0]["approval_reason"] == "hook requires confirmation"
    assert prompter.actions[0]["command_identity"] == "git"


def test_guarded_persistent_approval_trusts_only_that_command() -> None:
    persisted = []
    prompter = FakePrompter(
        ApprovalDecision(approved=True, always_approve_command=True)
    )
    policy = ApprovalPolicy(
        "guarded",
        prompter=prompter,
        persist_trusted_command=persisted.append,
    )

    assert policy.check(
        {"tool": "run_command", "args": {"command": "git status"}}
    ).approved
    assert policy.check(
        {"tool": "run_command", "args": {"command": "git reset --hard"}}
    ).approved
    prompter.decision = ApprovalDecision(approved=True)
    assert policy.check(
        {"tool": "run_command", "args": {"command": "python script.py"}}
    ).approved

    assert persisted == ["git"]
    assert [action["command_identity"] for action in prompter.actions] == [
        "git",
        "python",
    ]


def test_guarded_call_operator_trusts_only_invoked_program() -> None:
    persisted = []
    prompter = FakePrompter(
        ApprovalDecision(approved=True, always_approve_command=True)
    )
    policy = ApprovalPolicy(
        "guarded",
        prompter=prompter,
        persist_trusted_command=persisted.append,
    )

    assert policy.check(
        {
            "tool": "run_command",
            "args": {"command": '& "C:\\Program Files\\Tool\\tool.exe" --check'},
        }
    ).approved
    prompter.decision = ApprovalDecision(approved=True)
    assert policy.check(
        {
            "tool": "run_command",
            "args": {"command": "& Remove-Item -Recurse important"},
        }
    ).approved

    assert persisted == ["c:\\program files\\tool\\tool.exe"]
    assert [action["command_identity"] for action in prompter.actions] == [
        "c:\\program files\\tool\\tool.exe",
        "remove-item",
    ]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("  GIT status", "git"),
        ('"C:\\Program Files\\tool.exe" --check', "c:\\program files\\tool.exe"),
        ("'pytest' -q", "pytest"),
        ("   ", None),
    ],
)
def test_command_identity_uses_only_normalized_first_token(command, expected) -> None:
    assert command_identity(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            '& "C:\\Program Files\\Tool\\tool.exe" --check',
            "c:\\program files\\tool\\tool.exe",
        ),
        ("& Remove-Item -Recurse important", "remove-item"),
        (
            "& 'C:\\Program Files\\Tool\\tool.exe' --check",
            "c:\\program files\\tool\\tool.exe",
        ),
    ],
)
def test_command_identity_skips_powershell_call_operator(command, expected) -> None:
    assert command_identity(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "&",
        "& $command",
        "& ${command}",
        "& $(Get-Command tool)",
        "& (Get-Command tool)",
        "& { Get-Command tool }",
        "& @commands",
        "&\nRemove-Item important",
        '& "$command" --check',
        '& "C:\\Tools\\$variant\\tool.exe" --check',
        "& tool`-$variant",
    ],
)
def test_dynamic_call_operator_has_no_trustable_identity(command) -> None:
    assert command_identity(command) is None


def test_call_operator_cannot_be_persisted_as_trusted_identity() -> None:
    persisted = []
    prompter = FakePrompter(
        ApprovalDecision(approved=True, always_approve_command=True)
    )
    policy = ApprovalPolicy(
        "guarded",
        prompter=prompter,
        trusted_commands=("&",),
        persist_trusted_command=persisted.append,
    )

    assert policy.check(
        {"tool": "run_command", "args": {"command": "&"}}
    ).approved

    assert persisted == []
    assert prompter.actions[0]["approval_reason"] == "Command identity is missing"
    assert "command_identity" not in prompter.actions[0]
    assert "allow_persistent_command_approval" not in prompter.actions[0]


def test_hook_reason_overrides_trusted_call_operator_identity() -> None:
    action = {
        "tool": "run_command",
        "args": {"command": "& git status"},
        "approval_reason": "hook requires confirmation",
    }
    prompter = FakePrompter(ApprovalDecision(approved=True))
    policy = ApprovalPolicy(
        "guarded", prompter=prompter, trusted_commands=("git",)
    )

    assert policy.check(action).approved is True
    assert prompter.actions == [action]


def test_approval_policy_always_approves_when_prompter_approves() -> None:
    action = {"tool": "read_file"}
    prompter = FakePrompter(ApprovalDecision(approved=True))

    decision = ApprovalPolicy("always", prompter=prompter).check(action)

    assert decision.approved is True
    assert decision.reason == ""
    assert prompter.actions == [action]


def test_approval_policy_always_prompts_even_for_trusted_command() -> None:
    action = {"tool": "run_command", "args": {"command": "git status"}}
    prompter = FakePrompter(ApprovalDecision(approved=True))

    decision = ApprovalPolicy(
        "always", prompter=prompter, trusted_commands=("git",)
    ).check(action)

    assert decision.approved is True
    assert prompter.actions == [action]


def test_approval_policy_always_denies_when_prompter_denies() -> None:
    action = {"tool": "read_file"}
    prompter = FakePrompter(ApprovalDecision(approved=False))

    decision = ApprovalPolicy("always", prompter=prompter).check(action)

    assert decision.approved is False
    assert decision.reason == USER_DENIED_TOOL_ACTION
    assert decision.instruction == ""
    assert prompter.actions == [action]


def test_approval_policy_preserves_denial_instruction() -> None:
    action = {"tool": "run_command"}
    prompter = FakePrompter(
        ApprovalDecision(approved=False, instruction="Use pathlib instead")
    )

    decision = ApprovalPolicy("always", prompter=prompter).check(action)

    assert decision.approved is False
    assert decision.reason == USER_DENIED_TOOL_ACTION
    assert decision.instruction == "Use pathlib instead"
    assert prompter.actions == [action]
