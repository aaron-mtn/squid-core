"""Generic Core Agent behavior."""

from squid_core.platform_context import PlatformContext
from squid_core.skills import Skill
from squid_core.agent.prompts import PromptDefaults, correction_message, system_prompt
from squid_core.tools.builtins import TOOLS


INVALID_REPLY_EXPLANATIONS = {
    "empty_final_reply": "the model replied with no tool call and no final content",
    "malformed_tool_call": "the model produced an invalid tool call payload",
}


class Agent:
    """Own generic agent state, prompt creation, tools, and completion rules."""

    def __init__(
        self,
        task: str,
        skill: Skill | None = None,
        tools: dict | None = None,
        platform_context: PlatformContext | None = None,
        workspace: str | None = None,
        prompt_defaults: PromptDefaults | None = None,
    ) -> None:
        self.task = task
        self.skill = skill
        self.skill_name = skill.name if skill is not None else None
        self.tools = TOOLS if tools is None else tools
        self.platform_context = platform_context
        self.workspace = workspace
        self.prompt_defaults = prompt_defaults or PromptDefaults()

    def initial_messages(self) -> list[dict]:
        return [
            {
                "role": "system",
                "content": system_prompt(
                    self.prompt_defaults,
                    self.skill,
                    self.platform_context,
                    self.workspace,
                ),
            },
            {"role": "user", "content": self.task},
        ]

    def follow_up_message(self, task: str) -> dict:
        return {"role": "user", "content": task}

    def tool_definitions(self) -> list[dict]:
        return [tool["definition"]() for tool in self.tools.values()]

    def should_stop(self, session) -> bool:
        for message in reversed(session.messages):
            if message["role"] == "user":
                return False
            if message["role"] != "assistant":
                continue
            return not message.get("tool_calls") and bool(message.get("content", "").strip())
        return False

    def validate_reply(self, reply: dict) -> dict:
        actions = reply["actions"]
        content = reply["message"]["content"].strip()
        format_error = reply.get("format_error", "").strip()
        if format_error:
            return {"ok": False, "reason": "malformed_tool_call"}
        if len(actions) == 0:
            if content:
                return {"ok": True, "reason": ""}
            return {"ok": False, "reason": "empty_final_reply"}
        return {"ok": True, "reason": ""}

    def correction_message_for(self, reason: str) -> dict:
        return {"role": "user", "content": correction_message(reason)}

    def failure_message_for_invalid_replies(self, reason: str, count: int) -> dict:
        explanation = INVALID_REPLY_EXPLANATIONS.get(
            reason, "the model produced repeated invalid replies"
        )
        return {
            "role": "assistant",
            "content": (
                f"{self.prompt_defaults.agent_name} stopped after {count} consecutive invalid replies.\n"
                f"Last invalid reply reason: {reason}\n"
                f"Explanation: {explanation}"
            ),
        }
