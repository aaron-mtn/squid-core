# squid_core/tools/builtins/__init__.py

"""Owns the tiny tool registry; must not own policy, execution flow, or dynamic plugin machinery."""

from .apply_patch import TOOL_NAME as APPLY_PATCH_NAME, execute as apply_patch, tool_definition as apply_patch_definition
from .read_file import TOOL_NAME as READ_FILE_NAME, execute as read_file, tool_definition as read_file_definition
from .run_command import TOOL_NAME as RUN_COMMAND_NAME, execute as run_command, tool_definition as run_command_definition
from .write_file import TOOL_NAME as WRITE_FILE_NAME, execute as write_file, tool_definition as write_file_definition


TOOLS = {
    READ_FILE_NAME: {
        "execute": read_file,
        "definition": read_file_definition,
    },
    WRITE_FILE_NAME: {
        "execute": write_file,
        "definition": write_file_definition,
    },
    APPLY_PATCH_NAME: {
        "execute": apply_patch,
        "definition": apply_patch_definition,
    },
    RUN_COMMAND_NAME: {
        "execute": run_command,
        "definition": run_command_definition,
    },
}
