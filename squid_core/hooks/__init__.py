"""Lifecycle hook types, execution, and metadata loading."""

from .loader import (
    HookInfo,
    HookLoadError,
    list_user_hooks,
    load_hook_infos,
    load_user_hooks,
    read_hook_info,
)
from .no_progress import (
    DEFAULT_REPEATED_FAILURE_THRESHOLD,
    RepeatedFailureNudge,
)
from .post_verification import (
    CURRENT_VERIFICATION_STATE_GUIDANCE,
    DEFAULT_POST_VERIFICATION_THRESHOLD,
    POST_VERIFICATION_GUIDANCE,
    PostVerificationNudge,
)
from .redundant_read import (
    DEFAULT_REPEATED_READ_THRESHOLD,
    RepeatedReadNudge,
)
from .runner import (
    BeforeAcceptFinalContext,
    BeforeModelTurnContext,
    BeforeRunContext,
    BeforeToolExecuteContext,
    CompactionAppliedContext,
    FollowUpRunRequest,
    HookResponse,
    HookRunner,
    PromptContextBlock,
    ReadResultTrimmedContext,
    RunFinishedContext,
    ToolResultContext,
)
from .too_many_reads import (
    DEFAULT_FIRST_NUDGE_LINES,
    DEFAULT_FIRST_NUDGE_READS,
    DEFAULT_STRONG_NUDGE_LINES,
    DEFAULT_STRONG_NUDGE_READS,
    TOO_MANY_READS_GUIDANCE,
    TOO_MANY_READS_STRONG_GUIDANCE,
    TooManyReadsNudge,
)

__all__ = [
    "BeforeAcceptFinalContext",
    "BeforeModelTurnContext",
    "BeforeRunContext",
    "BeforeToolExecuteContext",
    "CompactionAppliedContext",
    "FollowUpRunRequest",
    "HookInfo",
    "HookLoadError",
    "HookResponse",
    "HookRunner",
    "PromptContextBlock",
    "ReadResultTrimmedContext",
    "CURRENT_VERIFICATION_STATE_GUIDANCE",
    "DEFAULT_POST_VERIFICATION_THRESHOLD",
    "POST_VERIFICATION_GUIDANCE",
    "PostVerificationNudge",
    "DEFAULT_REPEATED_FAILURE_THRESHOLD",
    "RepeatedFailureNudge",
    "DEFAULT_REPEATED_READ_THRESHOLD",
    "RepeatedReadNudge",
    "RunFinishedContext",
    "ToolResultContext",
    "DEFAULT_FIRST_NUDGE_LINES",
    "DEFAULT_FIRST_NUDGE_READS",
    "DEFAULT_STRONG_NUDGE_LINES",
    "DEFAULT_STRONG_NUDGE_READS",
    "TOO_MANY_READS_GUIDANCE",
    "TOO_MANY_READS_STRONG_GUIDANCE",
    "TooManyReadsNudge",
    "list_user_hooks",
    "load_hook_infos",
    "load_user_hooks",
    "read_hook_info",
]
