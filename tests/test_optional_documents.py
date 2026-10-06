"""Verify base runtime imports and optional format failures without parsers."""

import builtins
from pathlib import Path
import subprocess
import sys
import tomllib
from zipfile import ZipFile

import pytest

from squid_core.file_readers.registry import reader_for_suffix
from squid_core.file_readers.text import read_text
from squid_core.image_content import ImageValidationError, inspect_image_bytes
from squid_core.storage import SessionStore
from squid_core.tools.builtins.read_file import execute as read_file


DOCUMENT_ROOTS = {"epub_utils", "odf", "openpyxl", "docx", "PIL", "pymupdf", "fitz"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_documents(monkeypatch):
    """Block even cached imports so installed parsers cannot mask base failures."""
    original_import = builtins.__import__

    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.split(".")[0] in DOCUMENT_ROOTS:
            raise ModuleNotFoundError(f"Blocked optional dependency: {name}", name=name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)


def test_packaging_dependencies_are_optional() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == []
    assert project["optional-dependencies"]["documents"] == [
        "epub-utils==0.1.0a1", "odfpy", "openpyxl", "python-docx", "Pillow", "PyMuPDF",
    ]


def test_fresh_base_imports_construction_and_tool_schemas() -> None:
    # A fresh interpreter is essential: already-loaded Core modules would hide
    # eager imports. Block PyMuPDF's legacy fitz alias as well as its modern root.
    script = '''
import builtins
original_import = builtins.__import__
def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in {'epub_utils', 'odf', 'openpyxl', 'docx', 'PIL', 'pymupdf', 'fitz'}:
        raise ModuleNotFoundError(name, name=name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = blocked_import
import squid_core
from squid_core.agent import Agent
from squid_core.spawner import build_agent_run
from squid_core.tools.builtins import TOOLS
assert Agent('hello').initial_messages()[-1]['content'] == 'hello'
for name, tool in TOOLS.items():
    assert tool['definition']()['function']['name'] == name
'''
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_text_and_session_storage_without_documents(no_documents, tmp_path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("plain UTF-8 🦑\n", encoding="utf-8")
    assert read_file(tmp_path, str(source))["content"] == "plain UTF-8 🦑\n"
    store = SessionStore.create(tmp_path / "sessions", "base")
    store.append_message({"role": "user", "content": "hello"})
    assert store.load_messages()[-1]["content"] == "hello"


@pytest.mark.parametrize(
    ("suffix", "format_name"),
    [(".epub", "EPUB"), (".ods", "ODS"), (".xlsx", "XLSX"),
     (".docx", "DOCX"), (".pdf", "PDF"), (".png", "Image"),
     (".jpg", "Image"), (".jpeg", "Image"), (".webp", "Image")],
)
def test_known_formats_fail_with_install_guidance(
    no_documents, tmp_path, suffix, format_name,
) -> None:
    source = tmp_path / f"fixture{suffix}"
    # ZIP safety checks remain parser-independent and run before lazy imports.
    if suffix in {".epub", ".ods", ".xlsx", ".docx"}:
        with ZipFile(source, "w") as archive:
            archive.writestr("content.txt", "fixture")
    else:
        source.write_bytes(b"fixture")
    store = SessionStore.create(tmp_path / "sessions", "optional")
    assert reader_for_suffix(suffix) is not read_text
    assert reader_for_suffix(suffix.upper()) is reader_for_suffix(suffix)
    result = read_file(
        tmp_path, str(source), snapshot_asset=store.snapshot_asset,
        ensure_generated_image=store.ensure_generated_image,
    )
    assert result == {
        "ok": False, "tool": "read_file", "path": str(source),
        "error": f'{format_name} support requires optional document dependencies. '
                 'Install with: pip install "squid-core[documents]"',
    }


def test_image_validation_without_pillow(no_documents) -> None:
    with pytest.raises(ImageValidationError, match=r'squid-core\[documents\]'):
        inspect_image_bytes(b"fixture", ".png")


@pytest.mark.parametrize("cached", [False, True])
def test_generated_image_validation_without_pillow(no_documents, tmp_path, cached) -> None:
    store = SessionStore.create(tmp_path / "sessions", "generated")
    if cached:
        folder = store.assets_dir / "generated"
        folder.mkdir(parents=True)
        (folder / "page.png").write_bytes(b"fixture")
    with pytest.raises(ImageValidationError, match=r'squid-core\[documents\]'):
        store.ensure_generated_image("generated", "page.png", "key", lambda: b"fixture")
