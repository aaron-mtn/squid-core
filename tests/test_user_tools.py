"""Tests for discovery and lazy execution of user-defined tools."""

from pathlib import Path

import pytest

from squid_core.session import Session
from squid_core.tools import ToolExecutor
from squid_core.tools.user_tools import (
    UserTool,
    UserToolError,
    build_user_tool_registry,
    build_user_tool_registry_from_metadata,
    discover_user_tools,
    execute_user_tool,
    load_user_tool,
)


def _write_tool(home: Path, directory_name: str, metadata: str, source: str = "") -> Path:
    tool_dir = home / "tools" / directory_name
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.yaml").write_text(metadata, encoding="utf-8")
    if source:
        (tool_dir / "tool.py").write_text(source, encoding="utf-8")
    return tool_dir


def test_discovers_valid_user_tool_from_explicit_app_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_tool(
        home,
        "local_search",
        """name: local_search
description: Search local notes.
version: "1.2.0"
entrypoint: search.py
function: run_search
args:
  query:
    type: string
safety:
  network: false
""",
    )

    assert discover_user_tools(app_home=home) == {
        "local_search": UserTool(
            name="local_search",
            description="Search local notes.",
            version="1.2.0",
            entrypoint="search.py",
            function="run_search",
            args={"query": {"type": "string"}},
            safety={"network": False},
            source="user",
            editable=True,
        )
    }


@pytest.mark.parametrize(
    ("directory_name", "metadata"),
    [
        ("BadName", "name: BadName\n"),
        ("bad.name", "name: bad.name\n"),
        ("bad name", "name: bad name\n"),
    ],
)
def test_rejects_unsafe_names(
    tmp_path: Path,
    directory_name: str,
    metadata: str,
) -> None:
    tool_dir = _write_tool(tmp_path, directory_name, metadata)

    with pytest.raises(UserToolError, match="tool name must contain only"):
        load_user_tool(tool_dir)


def test_rejects_mismatch_between_folder_and_yaml_name(tmp_path: Path) -> None:
    tool_dir = _write_tool(tmp_path, "folder_name", "name: yaml_name\n")

    with pytest.raises(UserToolError, match="must match folder name"):
        load_user_tool(tool_dir)


def test_handles_missing_optional_fields_with_defaults(tmp_path: Path) -> None:
    tool_dir = _write_tool(tmp_path, "minimal", "name: minimal\n")

    assert load_user_tool(tool_dir) == UserTool(
        name="minimal",
        description="",
        version="",
        entrypoint="tool.py",
        function="run",
        args={},
        safety={},
        source="user",
        editable=True,
    )


def test_accepts_python_identifier_function_name(tmp_path: Path) -> None:
    tool_dir = _write_tool(
        tmp_path,
        "valid_function",
        "name: valid_function\nfunction: run_tool\n",
    )

    assert load_user_tool(tool_dir) == UserTool(
        name="valid_function",
        function="run_tool",
    )


@pytest.mark.parametrize("function", ["bad-name", "bad name", "123run"])
def test_discovery_skips_invalid_function_names(
    tmp_path: Path,
    function: str,
) -> None:
    home = tmp_path / "home"
    _write_tool(
        home,
        "invalid_function",
        f"name: invalid_function\nfunction: {function}\n",
    )
    _write_tool(home, "valid", "name: valid\n")

    assert discover_user_tools(app_home=home) == {"valid": UserTool(name="valid")}


@pytest.mark.parametrize("function", ["bad-name", "bad name", "123run"])
def test_rejects_invalid_function_names(tmp_path: Path, function: str) -> None:
    tool_dir = _write_tool(
        tmp_path,
        "invalid_function",
        f"name: invalid_function\nfunction: {function}\n",
    )

    with pytest.raises(UserToolError, match="function must be a valid Python identifier"):
        load_user_tool(tool_dir)


@pytest.mark.parametrize(
    "metadata",
    [
        "name malformed\n",
        '{"name": "broken"',
        "name: broken\nargs: [\n",
    ],
)
def test_handles_malformed_yaml_json_safely(tmp_path: Path, metadata: str) -> None:
    tool_dir = _write_tool(tmp_path, "broken", metadata)

    with pytest.raises(UserToolError):
        load_user_tool(tool_dir)


@pytest.mark.parametrize(
    ("metadata", "error"),
    [
        ("---\nname: broken\n---\n", "invalid metadata line"),
        ("name: broken\nargs:\n  values:\n    - one\n", "invalid metadata line"),
        (
            "name: broken\nargs:\n  type: object\n  properties:\n    value:\n      type: string\n",
            "top-level JSON Schema",
        ),
    ],
)
def test_rejects_unsupported_tool_metadata_shapes(
    tmp_path: Path,
    metadata: str,
    error: str,
) -> None:
    tool_dir = _write_tool(tmp_path, "broken", metadata)

    with pytest.raises(UserToolError, match=error):
        load_user_tool(tool_dir)


def test_discovery_skips_malformed_tools_independently(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_tool(home, "broken", "name malformed\n")
    _write_tool(home, "valid", "name: valid\n")

    assert discover_user_tools(app_home=home) == {"valid": UserTool(name="valid")}


def test_does_not_import_or_execute_tool_py_during_discovery(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    marker = home / "imported"
    _write_tool(
        home,
        "passive",
        "name: passive\nentrypoint: tool.py\n",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
    )

    assert discover_user_tools(app_home=home) == {"passive": UserTool(name="passive")}
    assert not marker.exists()


def test_user_tool_registry_imports_tool_py_only_during_execution(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    marker = home / "imported"
    _write_tool(
        home,
        "lazy",
        "name: lazy\n",
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("imported")

def run(args, context=None):
    return "done"
""",
    )

    registry = build_user_tool_registry(app_home=home)

    assert not marker.exists()
    assert registry["lazy"]["execute"](tmp_path)["message"] == "done"
    assert marker.read_text(encoding="utf-8") == "imported"


def test_user_tool_module_names_are_core_owned_fresh_and_isolated(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_tool(
        home,
        "module_identity",
        "name: module_identity\n",
        "def run(args, context=None):\n    return __name__\n",
    )
    execute = build_user_tool_registry(app_home=home)["module_identity"]["execute"]

    first_name = execute(tmp_path)["message"]
    second_name = execute(tmp_path)["message"]

    assert first_name.startswith("squid_core_dynamic_user_tool_module_identity_")
    assert "code_squid" not in first_name
    assert second_name != first_name


def test_explicit_user_tool_metadata_builds_executable_registry_without_discovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool_dir = _write_tool(
        tmp_path / "package",
        "package_echo",
        "name: package_echo\n",
        "def run(args, context=None):\n    return args['message']\n",
    )
    tool = load_user_tool(tool_dir, source="mod", editable=False)
    assert tool is not None
    monkeypatch.setattr(
        "squid_core.tools.user_tools.discover_user_tools",
        lambda: pytest.fail("explicit registry must not rediscover tools"),
    )

    registry = build_user_tool_registry_from_metadata(
        {tool.name: tool},
        session_id="mod-session",
    )

    assert registry["package_echo"]["source"] == "mod"
    assert registry["package_echo"]["execute"](tmp_path, message="hello")["message"] == "hello"


def test_explicit_user_tool_registry_does_not_import_source_until_execution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "imported"
    tool_dir = _write_tool(
        tmp_path / "package",
        "lazy_package_tool",
        "name: lazy_package_tool\n",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n"
        "def run(args, context=None):\n    return 'done'\n",
    )
    tool = load_user_tool(tool_dir)
    assert tool is not None

    registry = build_user_tool_registry_from_metadata({tool.name: tool})

    assert not marker.exists()
    assert registry[tool.name]["execute"](tmp_path)["message"] == "done"
    assert marker.exists()


def test_empty_tools_dir_returns_empty_dict(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "tools").mkdir(parents=True)

    assert discover_user_tools(app_home=home) == {}


def test_user_tool_registry_exposes_schema_from_metadata(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_tool(
        home,
        "local_search",
        """name: local_search
description: Search local notes.
args:
  query:
    type: string
    description: Search query
    required: true
""",
        "def run(args, context=None):\n    return None\n",
    )

    definition = build_user_tool_registry(app_home=home)["local_search"]["definition"]()
    parameters = definition["function"]["parameters"]

    assert definition["function"]["name"] == "local_search"
    assert definition["function"]["description"] == "Search local notes."
    assert parameters["properties"]["query"] == {
        "type": "string",
        "description": "Search query",
    }
    assert parameters["required"] == ["query"]


def test_selected_user_tool_executes_and_returns_normalized_dict(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_tool(
        home,
        "echo_args",
        "name: echo_args\n",
        """
def run(args, context=None):
    return {
        "ok": True,
        "message": args["message"],
        "context_repo": context["repo_root"],
        "context_tool": context["tool_name"],
        "context_session": context["session_id"],
    }
""",
    )
    registry = build_user_tool_registry(app_home=home, session_id="session-1")
    executor = ToolExecutor(
        repo_root=repo,
        session=Session(repo_root=repo),
        tools={"echo_args": registry["echo_args"]},
    )

    result = executor.execute(
        {
            "tool": "echo_args",
            "args": {"message": "hello"},
        }
    )

    assert result == {
        "ok": True,
        "tool": "echo_args",
        "message": "hello",
        "context_repo": str(repo),
        "context_tool": "echo_args",
        "context_session": "session-1",
    }


def test_selected_user_tool_string_result_is_wrapped(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_tool(
        home,
        "say_hi",
        "name: say_hi\n",
        "def run(args, context=None):\n    return 'hi'\n",
    )

    result = build_user_tool_registry(app_home=home)["say_hi"]["execute"](tmp_path)

    assert result == {
        "ok": True,
        "tool": "say_hi",
        "message": "hi",
        "data": {},
    }


def test_selected_user_tool_none_result_is_wrapped(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_tool(
        home,
        "noop",
        "name: noop\n",
        "def run(args, context=None):\n    return None\n",
    )

    result = build_user_tool_registry(app_home=home)["noop"]["execute"](tmp_path)

    assert result == {
        "ok": True,
        "tool": "noop",
        "message": "",
        "data": {},
    }


def test_selected_user_tool_exception_becomes_error_result(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_tool(
        home,
        "broken",
        "name: broken\n",
        "def run(args, context=None):\n    raise RuntimeError('boom')\n",
    )

    result = build_user_tool_registry(app_home=home)["broken"]["execute"](tmp_path)

    assert result == {
        "ok": False,
        "tool": "broken",
        "error": "boom",
    }


def test_user_tool_entrypoint_cannot_escape_tool_directory(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "tools" / "escape").mkdir(parents=True)
    (home / "tools" / "outside.py").write_text(
        "def run(args, context=None):\n    return 'escaped'\n",
        encoding="utf-8",
    )

    result = execute_user_tool(
        UserTool(name="escape", entrypoint="../outside.py"),
        tmp_path,
        {},
    )

    assert result["ok"] is False
    assert result["tool"] == "escape"
    assert "entrypoint must stay inside the tool directory" in result["error"]
