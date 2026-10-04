"""Core protocol and protected application-policy prompt regressions."""

from pathlib import Path

from squid_core.skills import PromptSectionOverride, Skill
from squid_core.agent import Agent


def _skill_with_replacements() -> Skill:
    return Skill(
        name="review",
        description="Review a change.",
        instructions="Focus on architecture.",
        allowed_tools=("read_file",),
        path=Path("SKILL.md"),
        prompt_sections={
            "SYSTEM_PROMPT_INTRO": PromptSectionOverride(
                section="SYSTEM_PROMPT_INTRO",
                mode="replace",
                content="Specialized intro.",
            ),
            "TOOL_USAGE_GUIDANCE": PromptSectionOverride(
                section="TOOL_USAGE_GUIDANCE",
                mode="replace",
                content="Use review tools carefully.",
            ),
            "FINAL_SUMMARY_REQUIREMENTS": PromptSectionOverride(
                section="FINAL_SUMMARY_REQUIREMENTS",
                mode="replace",
                content="Return review findings.",
            ),
        },
    )




def test_core_agent_starts_with_system_and_raw_authored_task() -> None:
    messages = Agent("  keep authored spacing  ").initial_messages()

    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[1] == {"role": "user", "content": "  keep authored spacing  "}


def test_core_agent_follow_up_is_raw_authored_user_message() -> None:
    message = Agent("first").follow_up_message("  follow up exactly  ")

    assert message == {"role": "user", "content": "  follow up exactly  "}


def test_active_skill_name_is_exposed() -> None:
    agent = Agent("review", skill=_skill_with_replacements())

    assert agent.skill_name == "review"


def test_skill_replacements_cannot_erase_core_protocol_or_narration() -> None:
    prompt = Agent(
        "review", skill=_skill_with_replacements()
    ).initial_messages()[0]["content"]

    assert "Batch multiple tool calls when it is efficient and the arguments are known." in prompt
    assert "could eliminate the need for a later call or materially change" in prompt
    assert "wait for the earlier result before issuing the later call" in prompt
    assert "A final response contains no tool call" in prompt
    assert "Prefer a brief natural note or narration before tool calls when useful." in prompt
    assert "A non-final turn uses one or more real tool calls." in prompt
    assert "[one or more independent tool calls]" in prompt


def test_default_prompt_allows_efficient_batches_without_speculative_calls() -> None:
    prompt = Agent("inspect it").initial_messages()[0]["content"]

    assert "A non-final turn uses one or more real tool calls." in prompt
    assert "Batch multiple tool calls when it is efficient and the arguments are known." in prompt
    assert (
        "If an earlier call's result could eliminate the need for a later call "
        "or materially change what that later call should do, wait for the "
        "earlier result before issuing the later call."
    ) in prompt
    assert "[one or more independent tool calls]" in prompt
    assert "each can be fully determined from the current context" not in prompt
    assert "If a later call depends on an earlier result" not in prompt




def test_prompt_encourages_narration_but_validation_does_not_require_it() -> None:
    agent = Agent("change it")
    prompt = agent.initial_messages()[0]["content"]
    reply = {
        "message": {"content": "   "},
        "actions": [{"tool": "read_file", "args": {}, "tool_call_id": "call_1"}],
    }

    assert "Prefer a brief natural note or narration before tool calls when useful." in prompt
    assert agent.validate_reply(reply) == {"ok": True, "reason": ""}
