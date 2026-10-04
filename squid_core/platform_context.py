"""Small, testable description of the local command environment."""

from __future__ import annotations

import os
import platform
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformContext:
    os_name: str
    shell_name: str
    shell_executable: str
    path_separator: str
    is_windows: bool


def detect_platform_context(
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    executable_lookup: Callable[[str], str | None] | None = None,
) -> PlatformContext:
    """Detect only the platform details needed by prompts and command execution."""

    platform_name = platform.system() if platform_name is None else platform_name
    environ = os.environ if environ is None else environ
    executable_lookup = shutil.which if executable_lookup is None else executable_lookup

    if platform_name == "Windows":
        shell_executable = executable_lookup("pwsh") or executable_lookup("powershell") or "powershell"
        return PlatformContext(
            os_name="Windows",
            shell_name="PowerShell",
            shell_executable=shell_executable,
            path_separator="\\",
            is_windows=True,
        )

    if platform_name == "Darwin":
        os_name = "macOS"
        fallback_shell = "/bin/zsh"
    else:
        os_name = "Linux"
        fallback_shell = "/bin/sh"

    shell_executable = environ.get("SHELL") or fallback_shell
    return PlatformContext(
        os_name=os_name,
        shell_name=Path(shell_executable).name,
        shell_executable=shell_executable,
        path_separator="/",
        is_windows=False,
    )
