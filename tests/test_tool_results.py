"""Tests for formatting tool execution results into transcript messages."""

import json

import pytest

from squid_core.tools import ToolResults


def test_messages_from_action_results_returns_tool_messages_when_lengths_match():
    tool_results = ToolResults()
    actions = [
        {"tool_call_id": "call_1"},
        {"tool_call_id": "call_2"},
    ]
    results = [
        {"ok": True},
        {"value": 42},
    ]

    messages = tool_results.messages_from_action_results(actions, results)

    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({"ok": True}, indent=2),
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "content": json.dumps({"value": 42}, indent=2),
        },
    ]


def test_messages_from_action_results_raises_when_more_actions_than_results():
    tool_results = ToolResults()
    actions = [
        {"tool_call_id": "call_1"},
        {"tool_call_id": "call_2"},
    ]
    results = [{"ok": True}]

    with pytest.raises(
        ValueError,
        match="Tool action/result length mismatch: 2 actions, 1 results",
    ):
        tool_results.messages_from_action_results(actions, results)


def test_messages_from_action_results_raises_when_more_results_than_actions():
    tool_results = ToolResults()
    actions = [{"tool_call_id": "call_1"}]
    results = [
        {"ok": True},
        {"value": 42},
    ]

    with pytest.raises(
        ValueError,
        match="Tool action/result length mismatch: 1 actions, 2 results",
    ):
        tool_results.messages_from_action_results(actions, results)


def test_message_from_action_result_includes_matching_tool_call_id():
    tool_results = ToolResults()

    message = tool_results.message_from_action_result(
        {"tool_call_id": "call_expected"},
        {"ok": True},
    )

    assert message["tool_call_id"] == "call_expected"
