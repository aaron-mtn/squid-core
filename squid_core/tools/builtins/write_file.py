"""Owns the write_file tool schema and file-writing behavior; must not own agent policy, loop control, or message formatting."""

from pathlib import Path


TOOL_NAME = "write_file"


# Return the tool definition shown to the model.
def tool_definition() -> dict:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Write complete UTF-8 content to an absolute path, creating or replacing the file. "
                "Use this when a full-file write is mechanically appropriate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file contents to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    }


# Write the full contents of a file and return the result in a consistent shape.
def execute(_repo_root: Path, path: str, content: str) -> dict:
    file_path = Path(path)
    if not file_path.is_absolute():
        return {
            "ok": False,
            "tool": TOOL_NAME,
            "path": path,
            "error": "write_file requires an absolute path",
        }

    characters_written = file_path.write_text(content, encoding="utf-8")

    return {
        "ok": True,
        "tool": TOOL_NAME,
        "path": path,
        "summary": f"Wrote {characters_written} characters",
        "characters_written": characters_written,
    }
