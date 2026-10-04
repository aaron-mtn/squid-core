"""Tests for metadata discovery and dynamic loading of user hooks."""

from pathlib import Path

import pytest

from squid_core.hooks import (
    HookInfo,
    HookLoadError,
    list_user_hooks,
    load_hook_infos,
    load_user_hooks,
)


def _write_hook(
    hooks_dir: Path,
    directory_name: str,
    metadata: str,
    source: str = "def create_hook(config=None):\n    return config\n",
) -> Path:
    hook_dir = hooks_dir / directory_name
    hook_dir.mkdir(parents=True)
    (hook_dir / "hook.yaml").write_text(metadata, encoding="utf-8")
    (hook_dir / "hook.py").write_text(source, encoding="utf-8")
    return hook_dir


def test_missing_hooks_dir_returns_empty_lists(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"

    assert list_user_hooks(hooks_dir) == []
    assert load_user_hooks(hooks_dir) == []


def test_list_enabled_hook_metadata(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hook_dir = _write_hook(
        hooks_dir,
        "example",
        """name: verification-guard
description: Blocks final responses unless verification is mentioned.
enabled: true
version: 2
""",
    )

    assert list_user_hooks(hooks_dir) == [
        HookInfo(
            name="verification-guard",
            description="Blocks final responses unless verification is mentioned.",
            enabled=True,
            version=2,
            path=hook_dir,
            source="user",
        )
    ]


def test_disabled_hook_is_listed_but_not_loaded(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    _write_hook(
        hooks_dir,
        "disabled",
        """name: disabled
enabled: false
version: 1
""",
        "raise AssertionError('disabled hook should not import')\n",
    )

    hooks = list_user_hooks(hooks_dir)

    assert hooks[0].enabled is False
    assert load_user_hooks(hooks_dir) == []


def test_enabled_hook_loads(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    _write_hook(
        hooks_dir,
        "enabled",
        "name: enabled\nenabled: true\n",
        """
class ExampleHook:
    marker = "loaded"

def create_hook(config=None):
    return ExampleHook()
""",
    )

    hooks = load_user_hooks(hooks_dir)

    assert len(hooks) == 1
    assert hooks[0].marker == "loaded"


def test_hook_module_names_are_core_owned_stable_and_isolated(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    source = "def create_hook(config=None):\n    return __name__\n"
    _write_hook(
        hooks_dir,
        "first",
        "name: first-hook\nenabled: true\n",
        source,
    )
    _write_hook(
        hooks_dir,
        "second",
        "name: second-hook\nenabled: true\n",
        source,
    )

    first_load = load_user_hooks(hooks_dir)
    second_load = load_user_hooks(hooks_dir)

    assert first_load == second_load
    assert len(set(first_load)) == 2
    assert all(name.startswith("squid_core_dynamic_user_hook_") for name in first_load)
    assert all("code_squid" not in name for name in first_load)


def test_selected_hook_info_loads_through_public_api(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    _write_hook(
        hooks_dir,
        "selected",
        "name: selected\nenabled: true\n",
        "def create_hook(config=None):\n    return 'selected:' + config['source']\n",
    )
    info = list_user_hooks(hooks_dir)[0]

    assert load_hook_infos([info]) == ["selected:user"]


def test_hook_metadata_listing_does_not_import_source(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    marker = tmp_path / "imported"
    _write_hook(
        hooks_dir,
        "passive",
        "name: passive\nenabled: true\n",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n"
        "def create_hook(config=None):\n    return object()\n",
    )

    infos = list_user_hooks(hooks_dir)

    assert [info.name for info in infos] == ["passive"]
    assert not marker.exists()
    assert len(load_hook_infos(infos)) == 1
    assert marker.exists()


def test_factory_receives_config(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hook_dir = _write_hook(
        hooks_dir,
        "configured",
        """name: configured
description: Uses metadata.
enabled: true
version: 3
""",
    )

    hooks = load_user_hooks(hooks_dir)

    assert hooks == [
        {
            "name": "configured",
            "description": "Uses metadata.",
            "enabled": True,
            "version": 3,
            "path": hook_dir,
            "source": "user",
        }
    ]


def test_factory_without_config_still_works(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    _write_hook(
        hooks_dir,
        "no-config",
        "name: no-config\nenabled: true\n",
        "def create_hook():\n    return 'loaded without config'\n",
    )

    assert load_user_hooks(hooks_dir) == ["loaded without config"]


def test_missing_create_hook_raises(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    _write_hook(
        hooks_dir,
        "missing-factory",
        "name: missing-factory\nenabled: true\n",
        "VALUE = 1\n",
    )

    with pytest.raises(HookLoadError, match="missing callable create_hook"):
        load_user_hooks(hooks_dir)


def test_malformed_metadata_raises(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    _write_hook(
        hooks_dir,
        "bad",
        "name bad\nenabled: true\n",
    )

    with pytest.raises(HookLoadError, match="invalid metadata line"):
        list_user_hooks(hooks_dir)


@pytest.mark.parametrize(
    "metadata",
    [
        "---\nname: bad\n---\n",
        "name: bad\nevents:\n  - run_finished\n",
    ],
)
def test_rejects_delimiters_and_list_syntax(tmp_path: Path, metadata: str) -> None:
    hooks_dir = tmp_path / "hooks"
    _write_hook(hooks_dir, "bad", metadata)

    with pytest.raises(HookLoadError, match="invalid metadata line"):
        list_user_hooks(hooks_dir)


def test_deterministic_load_order(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    _write_hook(
        hooks_dir,
        "b-hook",
        "name: b-hook\nenabled: true\n",
        "def create_hook(config=None):\n    return config['name']\n",
    )
    _write_hook(
        hooks_dir,
        "a-hook",
        "name: a-hook\nenabled: true\n",
        "def create_hook(config=None):\n    return config['name']\n",
    )

    assert load_user_hooks(hooks_dir) == ["a-hook", "b-hook"]
