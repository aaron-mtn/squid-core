"""Generic prompts for transcript compaction."""


COMPACTION_SYSTEM_PROMPT = (
    "You summarize prior agent interaction context for safe continuation. "
    "Preserve user-authored task requirements faithfully. Never invent, reinterpret, "
    "broaden, replace, or add task requirements."
)

COMPACTION_RETRY_CORRECTION = (
    "The previous compaction response was empty or invalid. Return a non-empty "
    "summary of the supplied conversation context."
)


def compaction_prompt(
    prior_context: str,
    authoritative_context: str = "",
    *,
    agent_context: str = "[]",
    user_context: str = "[]",
    retry_after_invalid_response: bool = False,
) -> str:
    retry_correction = ""
    if retry_after_invalid_response:
        retry_correction = f"\n\n{COMPACTION_RETRY_CORRECTION}"
    authoritative_section = ""
    if authoritative_context:
        authoritative_section = f"""

Authoritative host/runtime facts describing the state as of the end of the history being summarized:
{authoritative_context}

These host/runtime facts are authoritative. Do not contradict them in the summary.
"""
    return f"""
Summarize this prior interaction context so a later turn can continue safely.

Summarize the current working state, not an earlier state. Preserve the original and current task context, important findings, relevant files and resources, actions actually taken, tool and command results, verification results, failed attempts and corrections, the current next step, and remaining concrete risks or unknowns.
Use exact paths and identifiers when available. Do not invent facts, requested behavior, task requirements, or speculative risks. Do not reinterpret an instruction or infer a new requirement from implementation details, model output, or earlier work.
The agent/system and protected user sections are authoritative reference context, not historical events to compress. Do not include them in the summary merely because they are provided here. Use them to understand the run and preserve their requirements faithfully. User-authored messages inside the history being summarized are also authoritative task/instruction context.
Later events supersede earlier events. In particular, later failed verification supersedes earlier successful verification, and a later mutation makes earlier verification stale. Preserve unresolved failures and mistakes. Distinguish intended changes from changes actually performed.
Do not include a title or top-level heading. Return only the summary body.

Agent/system context (protected and retained verbatim outside the summary):
{agent_context}

Protected user task/instruction context (retained verbatim outside the summary):
{user_context}

History being summarized and replaced:
{prior_context}
{authoritative_section}
{retry_correction}
""".strip()
