"""Focused coverage for session-backed image reads and provider translation."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
from io import BytesIO
from pathlib import Path
from squid_core.model_response import ModelResponse
from types import SimpleNamespace

import pytest
from PIL import Image

from squid_core.model_adapter import ModelAdapter, ModelRequestError
from squid_core.image_content import VISION_UNAVAILABLE_MESSAGE
from squid_core.settings import ToolResultTrimmingSettings
from squid_core.storage import SessionStore
from squid_core.tool_result_trimming import render_messages_for_model
from squid_core.tools import ToolResults
from squid_core.tools.builtins.read_file import execute as read_file
from squid_core.tools.executor import ToolExecutor


image_reader_module = importlib.import_module("squid_core.file_readers.image")
atomic_json_module = importlib.import_module("squid_core.storage.atomic_json")


def image_bytes(image_format: str, *, size: tuple[int, int] = (3, 2)) -> bytes:
    """Create one tiny in-memory fixture without committing binary assets."""

    stream = BytesIO()
    Image.new("RGB", size, color=(220, 30, 40)).save(stream, format=image_format)
    return stream.getvalue()


def test_existing_text_and_range_reads_are_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")

    whole = read_file(tmp_path, str(source))
    ranged = read_file(
        tmp_path,
        str(source),
        ranges=[{"start_line": 2, "end_line": 2}],
    )

    assert whole["ok"] is True
    assert whole["content"] == "one\ntwo\nthree\n"
    assert whole["ranged"] is False
    assert ranged["ok"] is True
    assert "two" in ranged["content"]
    assert ranged["ranged"] is True


@pytest.mark.parametrize(
    ("suffix", "image_format", "mime_type", "canonical_suffix"),
    [
        (".png", "PNG", "image/png", ".png"),
        (".jpg", "JPEG", "image/jpeg", ".jpg"),
        (".jpeg", "JPEG", "image/jpeg", ".jpg"),
        (".webp", "WEBP", "image/webp", ".webp"),
    ],
)
def test_supported_image_read_snapshots_exact_bytes_and_metadata(
    tmp_path: Path,
    suffix: str,
    image_format: str,
    mime_type: str,
    canonical_suffix: str,
) -> None:
    store = SessionStore.create(tmp_path / "sessions", "image")
    source = tmp_path / f"fixture{suffix}"
    original = image_bytes(image_format)
    source.write_bytes(original)

    result = read_file(
        tmp_path,
        str(source),
        snapshot_asset=store.snapshot_asset,
    )

    digest = hashlib.sha256(original).hexdigest()
    assert result == {
        "ok": True,
        "tool": "read_file",
        "path": str(source),
        "media_type": "image",
        "image": {
            "version": 1,
            "asset_id": f"{digest}{canonical_suffix}",
            "sha256": digest,
            "mime_type": mime_type,
            "format": image_format,
            "width": 3,
            "height": 2,
            "size_bytes": len(original),
        },
    }
    assert store.load_asset(result["image"]["asset_id"]) == original


def test_image_read_rejects_ranges_corruption_and_safety_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore.create(tmp_path / "sessions", "errors")
    source = tmp_path / "fixture.png"
    source.write_bytes(image_bytes("PNG", size=(2, 2)))

    ranged = read_file(
        tmp_path,
        str(source),
        ranges=[{"start_line": 1, "end_line": 1}],
        snapshot_asset=store.snapshot_asset,
    )
    assert ranged["ok"] is False
    assert "line ranges" in ranged["error"]

    source.write_bytes(b"not an image")
    corrupt = read_file(tmp_path, str(source), snapshot_asset=store.snapshot_asset)
    assert corrupt["ok"] is False
    assert "invalid or corrupt image" in corrupt["error"]

    source.write_bytes(image_bytes("PNG", size=(2, 2)))
    monkeypatch.setattr(image_reader_module, "MAX_IMAGE_BYTES", 1)
    oversized = read_file(tmp_path, str(source), snapshot_asset=store.snapshot_asset)
    assert oversized["ok"] is False
    assert "maximum supported size" in oversized["error"]

    monkeypatch.setattr(image_reader_module, "MAX_IMAGE_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr("squid_core.image_content.MAX_IMAGE_PIXELS", 3)
    excessive_pixels = read_file(
        tmp_path,
        str(source),
        snapshot_asset=store.snapshot_asset,
    )
    assert excessive_pixels["ok"] is False
    assert "maximum supported count" in excessive_pixels["error"]


def test_image_read_rejects_extension_mismatch_and_snapshot_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture.png"
    source.write_bytes(image_bytes("JPEG"))

    mismatch = read_file(tmp_path, str(source), snapshot_asset=lambda *_args: "unused")
    assert mismatch["ok"] is False
    assert "does not match" in mismatch["error"]

    source.write_bytes(image_bytes("PNG"))

    def fail_snapshot(_data: bytes, _extension: str) -> str:
        raise OSError("disk unavailable")

    failed = read_file(tmp_path, str(source), snapshot_asset=fail_snapshot)
    assert failed["ok"] is False
    assert "could not snapshot image into the session" in failed["error"]

    unsupported = tmp_path / "fixture.gif"
    unsupported.write_bytes(image_bytes("GIF"))
    rejected = read_file(tmp_path, str(unsupported), snapshot_asset=lambda *_args: "unused")
    assert rejected["ok"] is False
    assert rejected["error"] == "unsupported image extension: .gif"


def test_image_snapshot_uses_same_directory_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore.create(tmp_path / "sessions", "atomic")
    original_replace = atomic_json_module._replace_with_permission_retry
    observed: list[tuple[Path, Path]] = []

    def observe_replace(source: Path, destination: Path) -> None:
        assert source.exists()
        assert not destination.exists()
        observed.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(
        atomic_json_module,
        "_replace_with_permission_retry",
        observe_replace,
    )
    asset_id = store.snapshot_asset(image_bytes("PNG"), ".png")

    assert len(observed) == 1
    temporary_path, destination = observed[0]
    assert temporary_path.parent == store.assets_dir
    assert destination == store.assets_dir / asset_id
    assert destination.exists()
    assert not temporary_path.exists()


def test_image_snapshot_deduplicates_and_survives_workspace_change(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path / "sessions", "deduplicate")
    source = tmp_path / "fixture.png"
    original = image_bytes("PNG")
    source.write_bytes(original)

    first = read_file(tmp_path, str(source), snapshot_asset=store.snapshot_asset)
    second = read_file(tmp_path, str(source), snapshot_asset=store.snapshot_asset)
    source.write_bytes(image_bytes("PNG", size=(4, 4)))

    assert first["image"]["asset_id"] == second["image"]["asset_id"]
    assert len(list(store.assets_dir.iterdir())) == 1
    assert store.load_asset(first["image"]["asset_id"]) == original


def test_asset_loading_rejects_paths_missing_files_and_corruption(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path / "sessions", "safe assets")
    data = image_bytes("PNG")
    asset_id = store.snapshot_asset(data, ".png")

    with pytest.raises(ValueError, match="invalid session asset ID"):
        store.load_asset("../outside.png")
    (store.assets_dir / asset_id).unlink()
    with pytest.raises(FileNotFoundError, match="Missing session image asset"):
        store.load_asset(asset_id)

    asset_id = store.snapshot_asset(data, ".png")
    (store.assets_dir / asset_id).write_bytes(b"changed")
    with pytest.raises(OSError, match="Corrupt session image asset"):
        store.load_asset(asset_id)


def test_asset_directory_containment_fails_closed(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path / "sessions", "containment")
    outside = tmp_path / "outside-assets"
    outside.mkdir()
    store.assets_dir = outside

    with pytest.raises(ValueError, match="escapes the session directory"):
        store.snapshot_asset(image_bytes("PNG"), ".png")


def image_tool_turn(result: dict, *, call_id: str = "image_call") -> list[dict]:
    action = {"tool": "read_file", "tool_call_id": call_id, "args": {}}
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        ToolResults().message_from_action_result(action, result),
    ]


def test_request_rendering_keeps_tool_text_and_injects_after_sibling_batch(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path / "sessions", "batch")
    source = tmp_path / "fixture.png"
    source.write_bytes(image_bytes("PNG"))
    result = read_file(tmp_path, str(source), snapshot_asset=store.snapshot_asset)
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "image", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "command", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
        ],
    }
    messages = [
        assistant,
        ToolResults().message_from_action_result(
            {"tool_call_id": "image"}, result
        ),
        ToolResults().message_from_action_result(
            {"tool_call_id": "command"},
            {"ok": True, "tool": "run_command", "output": "done"},
        ),
    ]

    rendered = render_messages_for_model(messages, ToolResultTrimmingSettings())

    assert [message["role"] for message in rendered] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert isinstance(rendered[1]["content"], str)
    assert "READ_FILE IMAGE OK" in rendered[1]["content"]
    assert rendered[3]["content"][1]["type"] == "image_asset"
    assert "base64" not in json.dumps(rendered)










@pytest.mark.parametrize("vision_supported", [True, False])
def test_fresh_image_read_obeys_active_model_vision_capability(
    tmp_path: Path,
    vision_supported: bool,
) -> None:
    store = SessionStore.create(tmp_path / "sessions", "fresh capability")
    source = tmp_path / "fixture.png"
    source.write_bytes(image_bytes("PNG"))
    session = SimpleNamespace(
        mark_file_read=lambda _path: None,
        mark_file_written=lambda _path: None,
    )
    executor = ToolExecutor(
        tmp_path,
        session,
        snapshot_asset=store.snapshot_asset,
        supports_image_input=lambda: vision_supported,
    )

    result = executor.execute(
        {"tool": "read_file", "args": {"path": str(source)}}
    )

    assert result["ok"] is vision_supported
    if vision_supported:
        assert result["media_type"] == "image"
        assert store.load_asset(result["image"]["asset_id"]) == source.read_bytes()
    else:
        assert result == {
            "ok": False,
            "tool": "read_file",
            "path": str(source),
            "error": VISION_UNAVAILABLE_MESSAGE,
        }
        assert not store.assets_dir.exists()


def test_non_vision_adapter_replaces_historical_image_without_mutating_it() -> None:
    class TextOnlyProvider:
        def __init__(self) -> None:
            self.payload = None

        def supports_image_input(self, _model: str) -> bool:
            return False

        def create_response(self, **payload):
            self.payload = payload
            return ModelResponse("continued", finish_reason="stop")

    historical = {
        "role": "user",
        "content": [
            {"type": "text", "text": "[image: old.png]"},
            {
                "type": "image_asset",
                "asset_id": "historical.png",
                "mime_type": "image/png",
            },
        ],
    }
    provider = TextOnlyProvider()
    adapter = ModelAdapter(provider, "text-only")

    reply = adapter.respond([historical], [])

    assert reply["message"]["content"] == "continued"
    assert provider.payload is not None
    assert "image_asset" not in json.dumps(provider.payload)
    assert VISION_UNAVAILABLE_MESSAGE in json.dumps(provider.payload)
    assert historical["content"][1]["type"] == "image_asset"






def test_workspace_boundary_blocks_image_before_asset_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes("PNG"))
    store = SessionStore.create(tmp_path / "sessions", "boundary")
    session = SimpleNamespace(mark_file_read=lambda _path: None, mark_file_written=lambda _path: None)
    executor = ToolExecutor(
        workspace,
        session,
        snapshot_asset=store.snapshot_asset,
    )

    result = executor.execute({"tool": "read_file", "args": {"path": str(outside)}})

    assert result["ok"] is False
    assert "outside the workspace root" in result["error"]
    assert not store.assets_dir.exists()
