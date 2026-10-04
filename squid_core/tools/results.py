import json


class ToolResults:
    """Owns formatting tool outputs back into model-visible messages; must not own tool execution, tool-call parsing, or coding-specific decisions."""

    # Convert one executed tool action and its result into a transcript message.
    # The tool_call_id links the tool result back to the assistant tool call
    # that requested it, which is required by stricter OpenAI-compatible
    # tool-calling templates.
    def message_from_action_result(self, action: dict, result: dict) -> dict:
        return {
            "role": "tool",
            "tool_call_id": action["tool_call_id"],
            "content": json.dumps(result, indent=2),
        }

    # Convert multiple executed tool actions and results into transcript
    # messages in order.
    def messages_from_action_results(self, actions: list[dict], results: list[dict]) -> list[dict]:
        if len(actions) != len(results):
            raise ValueError(
                "Tool action/result length mismatch: "
                f"{len(actions)} actions, {len(results)} results"
            )

        return [
            self.message_from_action_result(action, result)
            for action, result in zip(actions, results)
        ]
