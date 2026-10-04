"""Transport-independent completed model output shared by Core and providers."""

from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """One ordered call; Core alone parses and validates raw JSON arguments."""

    id: str | None
    name: str | None
    arguments: str


@dataclass
class TokenUsage:
    """Provider-reported token counts; missing counts remain unknown."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class ModelResponse:
    """Completed output, excluding transport metadata and provider errors."""

    content: str | None
    reasoning: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    response_id: str | None = None
