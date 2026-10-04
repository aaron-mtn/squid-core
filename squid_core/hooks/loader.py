"""Discover and load saved user hook objects from disk."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

@dataclass(frozen=True)
class HookInfo:
    name: str
    description: str = ""
    enabled: bool = False
    version: int = 1
    path: Path | None = None
    source: str = "user"


class HookLoadError(RuntimeError):
    pass


def list_user_hooks(
    hooks_dir: Path | None = None,
    *,
    app_home: Path | str | None = None,
) -> list[HookInfo]:
    """Return metadata for saved user hooks in deterministic directory order."""
    if hooks_dir is not None:
        root = Path(hooks_dir)
    elif app_home is not None:
        root = Path(app_home) / "hooks"
    else:
        return []
    if not root.exists():
        return []
    if not root.is_dir():
        raise HookLoadError(f"{root}: hooks path is not a directory")

    hooks = []
    hook_dirs = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=_path_sort_key,
    )
    for hook_dir in hook_dirs:
        hooks.append(read_hook_info(hook_dir))
    return hooks


def load_user_hooks(
    hooks_dir: Path | None = None,
    *,
    app_home: Path | str | None = None,
) -> list[object]:
    """Import enabled saved user hooks."""
    return load_hook_infos(list_user_hooks(hooks_dir, app_home=app_home))


def load_hook_infos(hook_infos: list[HookInfo]) -> list[object]:
    """Import enabled, explicitly selected hook metadata and return instances."""
    loaded_hooks = []

    for info in hook_infos:
        if not info.enabled:
            continue
        if info.path is None:
            raise HookLoadError(f"{info.name}: hook path is missing")

        module = _import_hook_module(info)
        factory = getattr(module, "create_hook", None)
        if not callable(factory):
            raise HookLoadError(
                f"{info.name} ({info.path}): missing callable create_hook"
            )

        try:
            if _accepts_config(factory):
                loaded_hooks.append(factory(config=_metadata_dict(info)))
            else:
                loaded_hooks.append(factory())
        except Exception as error:
            raise HookLoadError(
                f"{info.name} ({info.path}): create_hook failed: {error}"
            ) from error

    return loaded_hooks


def read_hook_info(
    hook_dir: Path,
    *,
    source: str = "user",
    enabled_override: bool | None = None,
) -> HookInfo:
    metadata_path = hook_dir / "hook.yaml"
    source_path = hook_dir / "hook.py"

    if not metadata_path.is_file():
        raise HookLoadError(f"{hook_dir}: missing hook.yaml")
    if not source_path.is_file():
        raise HookLoadError(f"{hook_dir}: missing hook.py")

    metadata = _parse_metadata_file(metadata_path)
    name = _metadata_string(metadata, "name", metadata_path).strip()
    if not name:
        raise HookLoadError(f"{metadata_path}: missing required metadata field: name")
    description = _metadata_string(metadata, "description", metadata_path)
    enabled = (
        enabled_override
        if enabled_override is not None
        else _metadata_bool(metadata, "enabled", metadata_path, default=False)
    )
    version = _metadata_int(metadata, "version", metadata_path, default=1)

    return HookInfo(
        name=name,
        description=description,
        enabled=enabled,
        version=version,
        path=hook_dir,
        source=source,
    )


def _parse_metadata_file(path: Path) -> dict[str, str | bool | int]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HookLoadError(f"{path}: could not read hook metadata: {error}") from error

    metadata: dict[str, str | bool | int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise HookLoadError(f"{path}:{line_number}: invalid metadata line")

        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise HookLoadError(f"{path}:{line_number}: invalid empty metadata key")
        metadata[key] = _parse_metadata_value(raw_value.strip())

    return metadata


def _parse_metadata_value(value: str) -> str | bool | int:
    unquoted = value.strip().strip("\"'")
    lowered = unquoted.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if unquoted and unquoted.lstrip("-").isdigit():
        return int(unquoted)
    return unquoted


def _metadata_string(
    metadata: dict[str, str | bool | int],
    key: str,
    path: Path,
) -> str:
    value = metadata.get(key, "")
    if isinstance(value, bool):
        raise HookLoadError(f"{path}: {key} must be a string")
    return str(value)


def _metadata_bool(
    metadata: dict[str, str | bool | int],
    key: str,
    path: Path,
    *,
    default: bool,
) -> bool:
    value = metadata.get(key, default)
    if not isinstance(value, bool):
        raise HookLoadError(f"{path}: {key} must be true or false")
    return value


def _metadata_int(
    metadata: dict[str, str | bool | int],
    key: str,
    path: Path,
    *,
    default: int,
) -> int:
    value = metadata.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HookLoadError(f"{path}: {key} must be an integer")
    return value


def _import_hook_module(info: HookInfo) -> ModuleType:
    assert info.path is not None
    source_path = info.path / "hook.py"
    module_name = _module_name(info)

    try:
        spec = importlib.util.spec_from_file_location(module_name, source_path)
        if spec is None or spec.loader is None:
            raise HookLoadError(
                f"{info.name} ({source_path}): could not create import spec"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except HookLoadError:
        raise
    except Exception as error:
        sys.modules.pop(module_name, None)
        raise HookLoadError(
            f"{info.name} ({source_path}): failed to import hook: {error}"
        ) from error


def _module_name(info: HookInfo) -> str:
    assert info.path is not None
    digest = hashlib.sha256(str(info.path.resolve()).encode("utf-8")).hexdigest()[:16]
    safe_name = "".join(
        character if character.isalnum() else "_" for character in info.name
    )
    return f"squid_core_dynamic_user_hook_{safe_name}_{digest}"


def _accepts_config(factory: Any) -> bool:
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return True

    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if (
            parameter.name == "config"
            and parameter.kind != inspect.Parameter.POSITIONAL_ONLY
        ):
            return True
    return False


def _metadata_dict(info: HookInfo) -> dict[str, Any]:
    return {
        "name": info.name,
        "description": info.description,
        "enabled": info.enabled,
        "version": info.version,
        "path": info.path,
        "source": info.source,
    }


def _path_sort_key(path: Path) -> tuple[str, str]:
    return (path.name.lower(), path.name)
