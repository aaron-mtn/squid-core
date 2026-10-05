"""Protected agent protocol and prompt assembly."""

from dataclasses import dataclass

from squid_core.platform_context import PlatformContext
from squid_core.skills import Skill
from squid_core.tools.builtins import TOOLS


@dataclass(frozen=True)
class PromptDefaults:
    """Small, explicit application prompt surface consumed by Core."""

    agent_name: str = "Agent"
    agent_identity: str = "You are an agent working on a user-authored task."
    baseline_policy: str = ""
    tool_usage_guidance: str = ""
    final_summary_requirements: str = ""


PROTECTED_RESPONSE_PROTOCOL = """
## Response protocol

- A non-final turn uses one or more real tool calls.
- Batch multiple tool calls when it is efficient and the arguments are known.
- If an earlier call's result could eliminate the need for a later call or materially change what that later call should do, wait for the earlier result before issuing the later call.
- A final response contains no tool call and non-empty human-readable content.
- Finish when the task is complete.
""".strip()


PROTECTED_VERIFICATION_GUIDANCE = """
## Verification honesty

Report only what you actually verified, and make the conclusion match the evidence.

A syntax or build check does not prove runtime behavior. Tests did not pass unless they ran successfully. Interactive or visual behavior is unverified unless exercised.

State partial or unavailable verification plainly.
""".strip()


PROTECTED_RESPONSE_EXAMPLES = """
## Valid response examples

Valid non-final response:

I found the requested information. I am checking one remaining detail.
[one or more independent tool calls]

Valid final response:

The task is complete. I checked the requested information; one detail could not be independently verified.
""".strip()


def prompt_section(
    section_name: str,
    default: str,
    skill: Skill | None = None,
) -> str:
    """Apply a supported Skill section with literal append/replace semantics."""

    if skill is None:
        return default

    override = skill.prompt_sections.get(section_name)
    if override is None:
        return default

    if override.mode == "replace":
        return override.content

    if override.mode == "append":
        return f"{default}\n\n{override.content}"

    raise ValueError(
        f"Unsupported prompt section override mode: {override.mode}"
    )


def tool_usage_guidance(
    default: str,
    skill: Skill | None = None,
) -> str:
    """Retain application discipline unless a Skill explicitly replaces it."""

    return prompt_section("TOOL_USAGE_GUIDANCE", default, skill)


def skill_prompt_block(skill: Skill | None = None) -> str:
    """Expose the selected Skill instructions without changing runtime protocol."""

    if skill is None:
        return ""

    return f"""
## Active Skill: {skill.name}

Description:
{skill.description}

Skill instructions:
{skill.instructions}
""".strip()


def execution_environment_prompt(
    platform_context: PlatformContext,
    workspace: str,
    tools: dict | None = None,
) -> str:
    """Describe environment facts and only the active built-in tool capabilities."""

    active_tools = TOOLS if tools is None else tools
    lines = [
        "## Execution Environment",
        "",
        f"Operating system: {platform_context.os_name}",
        f"Preferred shell: {platform_context.shell_name}",
        f"Path separator: {platform_context.path_separator}",
        f"Workspace: {workspace}",
        "",
    ]

    # Environment facts remain useful even when the agent cannot execute commands.
    if "run_command" in active_tools:
        lines.append("Use commands, quoting, paths, and utilities valid for this environment.")

    if "run_command" in active_tools and platform_context.is_windows:
        lines.extend(
            [
                "run_command already executes in PowerShell. Write PowerShell commands directly; do not wrap them in `powershell -Command`.",
                "Use PowerShell syntax, paths, and commands, not Bash-only syntax.",
            ]
        )
    elif "run_command" in active_tools:
        lines.extend(
            [
                f"run_command executes using {platform_context.shell_name}. Write commands directly for that shell.",
                f"Use commands and separators compatible with {platform_context.shell_name}.",
                "Do not assume Windows PowerShell commands or Windows path syntax are available.",
            ]
        )

    # File preferences belong to the host; Core states only available capabilities.
    for name, purpose in (
        ("read_file", "reading files"),
        ("apply_patch", "focused file edits"),
        ("write_file", "writing files"),
    ):
        if name in active_tools:
            lines.append(f"{name} is available for {purpose}.")

    return "\n".join(lines)


def system_prompt(
    defaults: PromptDefaults | None = None,
    skill: Skill | None = None,
    platform_context: PlatformContext | None = None,
    workspace: str | None = None,
    tools: dict | None = None,
) -> str:
    """Assemble host policy around the protected, task-neutral runtime protocol."""
    defaults = defaults or PromptDefaults()

    identity = prompt_section(
        "AGENT_IDENTITY",
        defaults.agent_identity,
        skill,
    )

    environment = (
        execution_environment_prompt(platform_context, workspace, tools)
        if platform_context is not None and workspace is not None
        else ""
    )

    final_guidance = prompt_section(
        "FINAL_SUMMARY_REQUIREMENTS",
        defaults.final_summary_requirements,
        skill,
    )

    sections = [
        identity,
        defaults.baseline_policy,
        environment,
        skill_prompt_block(skill),
        tool_usage_guidance(defaults.tool_usage_guidance, skill),
        PROTECTED_RESPONSE_PROTOCOL,
        PROTECTED_VERIFICATION_GUIDANCE,
        final_guidance,
        PROTECTED_RESPONSE_EXAMPLES,
    ]

    return "\n\n".join(
        section for section in sections if section.strip()
    ).strip()


CORRECTION_MESSAGES = {
    "empty_final_reply": """
Format error: your last response had no tool call and no final content.

If the task is complete, reply with a concise non-empty final response and no tool call.

If work remains, choose one or more real tool calls for the immediate next step. Batch calls only when both their arguments and their necessity are already known from the current context. If an earlier result could eliminate or materially change a later call, wait for it before issuing that call.

Do not restate the plan or do extra work unless concrete work remains.
""".strip(),

    "malformed_tool_call": """
Format error: your last response included an invalid tool call.

Use one or more available tools with valid arguments for the immediate next step. Batch calls only when both their arguments and their necessity are already known from the current context. If an earlier result could eliminate or materially change a later call, wait for it before issuing that call.
""".strip(),
}


def correction_message(reason: str) -> str:
    """Return task-neutral recovery instructions for an invalid model reply."""

    return CORRECTION_MESSAGES.get(
        reason,
        (
            "Your last response was invalid. Reply with one or more valid "
            "tool calls for the immediate next step, or a non-empty final "
            "response with no tool call."
        ),
    )
