"""Generic Squid Core transcript compaction."""

from squid_core.compaction.compactor import Compactor
from squid_core.compaction.prompts import COMPACTION_SYSTEM_PROMPT, compaction_prompt

__all__ = ["Compactor", "COMPACTION_SYSTEM_PROMPT", "compaction_prompt"]
