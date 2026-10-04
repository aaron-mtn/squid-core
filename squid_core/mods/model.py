"""Mod metadata models and validation constants."""

from dataclasses import dataclass, field
from pathlib import Path
import re

from squid_core.skills import Skill


SAFE_MOD_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SAFE_COMPONENT_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
ALLOWED_MOD_ACCENTS = (
    "cyan", "blue", "green", "yellow", "orange",
    "red", "pink", "purple", "white", "gray",
)
DEFAULT_MOD_ACCENT = "cyan"
MOD_FIELDS = {"name", "description", "default_skill", "accent", "hooks"}


@dataclass(frozen=True)
class Mod:
    """Package metadata and Skills keyed by their Mod-local directory IDs."""

    name: str
    description: str
    default_skill: str
    accent: str
    hooks: tuple[str, ...]
    path: Path
    skills: dict[str, Skill] = field(default_factory=dict)
    valid: bool = True
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedMod:
    """One active Skill with its tool registry and the Mod-wide Hooks."""

    metadata: Mod
    skill: Skill
    tools: dict
    hooks: tuple[object, ...]


class ModMetadataError(RuntimeError):
    pass


class ModResolutionError(ValueError):
    pass
