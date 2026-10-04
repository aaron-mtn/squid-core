# squid_core/tools/builtins/_truncation.py

"""Small shared text truncation helpers for tool outputs."""


def truncate_head_tail(text: str, max_chars: int = 12000) -> tuple[str, dict]:
    original_chars = len(text)
    if original_chars <= max_chars:
        return text, {
            "truncated": False,
            "original_chars": original_chars,
            "returned_chars": original_chars,
        }

    marker = "\n\n[warning: output truncated; showing beginning and end. rerun a narrower command if you need more detail]\n\n"
    keep_each = (max_chars - len(marker)) // 2
    truncated = text[:keep_each] + marker + text[-keep_each:]

    return truncated, {
        "truncated": True,
        "original_chars": original_chars,
        "returned_chars": len(truncated),
    }
