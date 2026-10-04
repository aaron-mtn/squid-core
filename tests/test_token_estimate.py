import json

from squid_core.compaction.token_estimate import (
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
)


def test_empty_text_estimates_zero_tokens() -> None:
    assert estimate_text_tokens("") == 0


def test_non_empty_short_text_estimates_at_least_one_token() -> None:
    assert estimate_text_tokens("hey") == 1


def test_longer_text_uses_length_divided_by_four() -> None:
    assert estimate_text_tokens("x" * 20) == 5


def test_message_estimate_counts_serialized_message_content() -> None:
    message = {"role": "user", "content": "hello"}
    serialized = json.dumps(message, ensure_ascii=False)

    assert estimate_message_tokens(message) == estimate_text_tokens(serialized)


def test_messages_estimate_sums_multiple_message_estimates() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    assert estimate_messages_tokens(messages) == sum(
        estimate_message_tokens(message) for message in messages
    )
