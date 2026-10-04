"""Discover saved user tool metadata without executing tool code."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import json
from pathlib import Path
import re
import uuid
from typing import Any

from .builtins._truncation import truncate_head_tail


SAFE_TOOL_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class UserTool:
    name: str
    description: str = ""
    version: str = ""
    entrypoint: str = "tool.py"
    function: str = "run"
    args: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    source: str = "user"
    editable: bool = True
    path: Path | None = field(default=None, compare=False)


class UserToolError(RuntimeError):
    pass


def discover_user_tools(
    tools_dir: Path | str | None = None,
    *,
    app_home: Path | str | None = None,
) -> dict[str, UserTool]:
    """Return valid user tool metadata in deterministic directory order."""
    if tools_dir is not None:
        root = Path(tools_dir)
    elif app_home is not None:
        root = Path(app_home) / "tools"
    else:
        return {}
    tools: dict[str, UserTool] = {}

    if not root.exists():
        pass
    elif not root.is_dir():
        raise UserToolError(f"{root}: tools path is not a directory")
    else:
        for tool_dir in sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=_path_sort_key,
        ):
            try:
                tool = load_user_tool(tool_dir)
            except UserToolError:
                continue
            if tool is not None:
                tools[tool.name] = tool

    return tools


def build_user_tool_registry(
    *,
    app_home: Path | str | None = None,
    repo_root: Path | str | None = None,
    session_id: str | None = None,
) -> dict[str, dict]:
    """Return executable registry entries for discovered user tools."""
    return build_user_tool_registry_from_metadata(
        discover_user_tools(app_home=app_home),
        repo_root=repo_root,
        session_id=session_id,
    )


def build_user_tool_registry_from_metadata(
    user_tools: dict[str, UserTool],
    *,
    repo_root: Path | str | None = None,
    session_id: str | None = None,
) -> dict[str, dict]:
    """Build executable registry entries from explicitly selected metadata.

    This is the host-facing seam for package resolvers that have already
    selected exact tool components. Constructing the registry stays lazy and
    does not import tool source; source is imported by ``execute_user_tool``.
    """
    repo_root = None if repo_root is None else Path(repo_root)
    return {
        name: _registry_entry(tool, repo_root=repo_root, session_id=session_id)
        for name, tool in user_tools.items()
    }


def load_user_tool(
    tool_dir: Path | str,
    *,
    source: str = "user",
    editable: bool = True,
) -> UserTool | None:
    """Load one user tool manifest as metadata only."""
    tool_dir = Path(tool_dir)
    metadata_path = tool_dir / "tool.yaml"
    if not metadata_path.is_file():
        return None

    folder_name = tool_dir.name
    _validate_tool_name(folder_name, tool_dir)

    metadata = _parse_metadata_file(metadata_path)
    name = _metadata_string(metadata, "name", metadata_path, required=True).strip()
    _validate_tool_name(name, metadata_path)
    if name != folder_name:
        raise UserToolError(
            f"{metadata_path}: tool name {name!r} must match folder name {folder_name!r}"
        )

    entrypoint = _metadata_string(
        metadata,
        "entrypoint",
        metadata_path,
        default="tool.py",
    ).strip()
    _validate_safe_filename(entrypoint, metadata_path, "entrypoint")

    function = _metadata_string(
        metadata,
        "function",
        metadata_path,
        default="run",
    ).strip()
    if not function:
        raise UserToolError(f"{metadata_path}: function must not be empty")
    if not function.isidentifier():
        raise UserToolError(
            f"{metadata_path}: function must be a valid Python identifier"
        )

    args = _metadata_object(metadata, "args", metadata_path)
    if "properties" in args or args.get("type") == "object" or "required" in args:
        raise UserToolError(
            f"{metadata_path}: args must map argument names directly to property mappings; "
            "top-level JSON Schema type/properties/required structure is not supported"
        )

    return UserTool(
        name=name,
        description=_metadata_string(metadata, "description", metadata_path),
        version=_metadata_string(metadata, "version", metadata_path),
        entrypoint=entrypoint,
        function=function,
        args=args,
        safety=_metadata_object(metadata, "safety", metadata_path),
        source=source,
        editable=editable,
        path=tool_dir,
    )


def _parse_metadata_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise UserToolError(f"{path}: could not read tool metadata: {error}") from error

    stripped = text.strip()
    if not stripped:
        raise UserToolError(f"{path}: tool metadata must not be empty")

    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise UserToolError(f"{path}: invalid JSON metadata: {error}") from error
        if not isinstance(data, dict):
            raise UserToolError(f"{path}: tool metadata must be an object")
        return data

    return _parse_simple_yaml_mapping(text, path)


def _registry_entry(
    tool: UserTool,
    *,
    repo_root: Path | None,
    session_id: str | None,
) -> dict:
    return {
        "execute": lambda current_repo_root, **args: execute_user_tool(
            tool,
            current_repo_root if repo_root is None else repo_root,
            args,
            session_id=session_id,
        ),
        "definition": lambda: user_tool_definition(tool),
        "source": tool.source,
    }


def user_tool_definition(tool: UserTool) -> dict:
    """Convert saved user-tool metadata to a model tool definition."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or f"Run the {tool.name} user tool.",
            "parameters": _user_tool_parameters(tool.args),
        },
    }


def execute_user_tool(
    tool: UserTool,
    repo_root: Path | str,
    args: dict[str, Any],
    *,
    session_id: str | None = None,
) -> dict:
    """Import and execute a discovered user tool with normalized results."""
    tool_dir = tool.path if tool.path is not None else Path(tool.name)
    try:
        entrypoint = _resolve_entrypoint(tool_dir, tool.entrypoint)
        module = _load_user_tool_module(tool.name, entrypoint)
        function = getattr(module, tool.function, None)
        if function is None or not callable(function):
            return _error_result(tool.name, f"Function not found: {tool.function}")

        context = {
            "repo_root": str(Path(repo_root)),
            "tool_name": tool.name,
        }
        if session_id:
            context["session_id"] = session_id

        result = function(args, context=context)
        return _normalize_user_tool_result(tool.name, result)
    except Exception as error:
        return _error_result(tool.name, str(error))


def _resolve_entrypoint(tool_dir: Path, entrypoint: str) -> Path:
    base_dir = tool_dir.resolve()
    entrypoint_path = (tool_dir / entrypoint).resolve()
    if entrypoint_path != base_dir and base_dir not in entrypoint_path.parents:
        raise UserToolError(
            f"{tool_dir / entrypoint}: entrypoint must stay inside the tool directory"
        )
    if not entrypoint_path.is_file():
        raise UserToolError(f"{entrypoint_path}: entrypoint file does not exist")
    return entrypoint_path


def _load_user_tool_module(tool_name: str, entrypoint: Path):
    module_name = f"squid_core_dynamic_user_tool_{tool_name}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, entrypoint)
    if spec is None or spec.loader is None:
        raise UserToolError(f"{entrypoint}: could not load user tool module")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_user_tool_result(tool_name: str, result: Any) -> dict:
    if isinstance(result, dict):
        normalized = dict(result)
        normalized.setdefault("tool", tool_name)
    elif isinstance(result, str):
        normalized = {
            "ok": True,
            "tool": tool_name,
            "message": result,
            "data": {},
        }
    elif result is None:
        normalized = {
            "ok": True,
            "tool": tool_name,
            "message": "",
            "data": {},
        }
    else:
        normalized = {
            "ok": True,
            "tool": tool_name,
            "message": str(result),
            "data": {},
        }

    if "ok" not in normalized:
        normalized["ok"] = True
    return _truncate_user_tool_result(normalized)


def _error_result(tool_name: str, error: str) -> dict:
    return {
        "ok": False,
        "tool": tool_name,
        "error": error,
    }


def _truncate_user_tool_result(result: dict) -> dict:
    truncated = dict(result)
    for key, value in list(truncated.items()):
        if not isinstance(value, str):
            continue
        value, metadata = truncate_head_tail(value)
        truncated[key] = value
        if metadata["truncated"]:
            truncated[f"{key}_truncated"] = True
            truncated[f"{key}_original_chars"] = metadata["original_chars"]
            truncated[f"{key}_returned_chars"] = metadata["returned_chars"]
    return truncated


def _user_tool_parameters(args: dict[str, Any]) -> dict:
    if not args:
        return {
            "type": "object",
            "properties": {},
        }

    properties = {}
    required = []
    for name, spec in args.items():
        if not isinstance(name, str):
            continue
        if isinstance(spec, dict):
            property_spec = dict(spec)
        else:
            property_spec = {"description": str(spec)}
        required_flag = bool(property_spec.pop("required", False))
        properties[name] = property_spec
        if required_flag:
            required.append(name)

    parameters = {
        "type": "object",
        "properties": properties,
    }
    if required:
        parameters["required"] = required
    return parameters


def _parse_simple_yaml_mapping(text: str, path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for line_number, line in enumerate(text.splitlines(), start=1):
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise UserToolError(f"{path}:{line_number}: tabs are not valid indentation")

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise UserToolError(f"{path}:{line_number}: invalid metadata line")

        indent = len(line) - len(line.lstrip(" "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise UserToolError(f"{path}:{line_number}: invalid empty metadata key")

        parent = stack[-1][1]
        raw_value = raw_value.strip()
        if raw_value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
            continue

        parent[key] = _parse_scalar(raw_value, path, line_number)

    return root


def _parse_scalar(value: str, path: Path, line_number: int) -> Any:
    if value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise UserToolError(
                f"{path}:{line_number}: invalid inline JSON value: {error}"
            ) from error

    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]

    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if value and value.lstrip("-").isdigit():
        return int(value)
    return value


def _metadata_string(
    metadata: dict[str, Any],
    key: str,
    path: Path,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    if key not in metadata:
        if required:
            raise UserToolError(f"{path}: missing required metadata field: {key}")
        return default

    value = metadata[key]
    if not isinstance(value, str):
        raise UserToolError(f"{path}: {key} must be a string")
    if required and not value.strip():
        raise UserToolError(f"{path}: missing required metadata field: {key}")
    return value


def _metadata_object(metadata: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = metadata.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UserToolError(f"{path}: {key} must be an object")
    return value


def _validate_tool_name(name: str, path: Path) -> None:
    if not SAFE_TOOL_NAME_RE.fullmatch(name):
        raise UserToolError(
            f"{path}: tool name must contain only lowercase letters, numbers, underscores, and hyphens"
        )


def _validate_safe_filename(value: str, path: Path, field_name: str) -> None:
    if not value:
        raise UserToolError(f"{path}: {field_name} must not be empty")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise UserToolError(f"{path}: {field_name} must not contain path separators")


def _path_sort_key(path: Path) -> tuple[str, str]:
    return (path.name.lower(), path.name)
