from pathlib import Path
from typing import Any


class Session:
    """Owns message history and tiny structured state; must not own model calls, tool execution, or recovery strategy."""

    # Initialize a new session with the repo root and empty tracking containers.
    # Each session starts with no messages, no files read, and no files written.
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.messages: list[dict[str, Any]] = []
        self.files_read: set[str] = set()
        self.files_written: set[str] = set()

    # Add a single message dictionary to the end of the transcript.
    # Use this when you only have one new message to record.
    def add_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    # Add multiple message dictionaries to the transcript in order.
    # The *messages argument lets you pass any number of messages at once.
    def add_messages(self, *messages: dict[str, Any]) -> None:
        self.messages.extend(messages)

    # Record that a file path was read during this session.
    # A set is used so the same path is only stored once.
    def mark_file_read(self, path: str) -> None:
        self.files_read.add(path)

    # Record that a file path was written during this session.
    # A set is used so the same path is only stored once.
    def mark_file_written(self, path: str) -> None:
        self.files_written.add(path)
