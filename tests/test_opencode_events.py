from test_wsl2_llm.opencode_events import normalize_opencode_event
from test_wsl2_llm.traces import final_message_from_events, usage_from_events


def test_text_events_become_final_agent_messages() -> None:
    events = [
        normalize_opencode_event({"type": "text", "part": {"text": "partial"}}),
        normalize_opencode_event({"type": "text", "part": {"text": "final answer"}}),
    ]

    assert events[-1]["type"] == "item.completed"
    assert events[-1]["event_type"] == "text"
    assert final_message_from_events(events) == "final answer"


def test_final_message_joins_parts_from_last_opencode_message() -> None:
    events = [
        normalize_opencode_event({"type": "text", "part": {"messageID": "old", "text": "old"}}),
        normalize_opencode_event({"type": "text", "part": {"messageID": "new", "text": "first "}}),
        normalize_opencode_event({"type": "text", "part": {"messageID": "new", "text": "second"}}),
    ]
    assert final_message_from_events(events) == "first second"


def test_tool_and_step_events_are_normalized() -> None:
    tool_event = normalize_opencode_event(
        {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "done"}}}
    )
    start_event = normalize_opencode_event({"type": "step_start", "part": {}})

    assert tool_event["type"] == "item.completed"
    assert tool_event["event_type"] == "tool_use"
    assert tool_event["item"] == {
        "type": "tool_call",
        "tool": "bash",
        "state": {"status": "done"},
    }
    assert start_event["type"] == "turn.started"
    assert start_event["event_type"] == "step_start"


def test_step_finish_accounts_for_cache_and_deduplicates_message_ids() -> None:
    raw = {
        "type": "step_finish",
        "part": {
            "messageID": "message-1",
            "tokens": {
                "input": 100,
                "output": 20,
                "reasoning": 7,
                "cache": {"read": 30, "write": 5},
            },
        },
    }
    normalized = normalize_opencode_event(raw)
    duplicate = normalize_opencode_event(raw)
    second_message = normalize_opencode_event(
        {
            "type": "step_finish",
            "part": {
                "messageID": "message-2",
                "tokens": {
                    "input": 4,
                    "output": 2,
                    "reasoning": 1,
                    "cache": {"read": 3, "write": 2},
                },
            },
        }
    )

    assert normalized["event_type"] == "step_finish"
    assert normalized["usage_source"] == "opencode_step_finish"
    assert normalized["usage_message_id"] == "message-1"
    assert normalized["usage"] == {
        "input_tokens": 135,
        "cached_input_tokens": 35,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 5,
        "output_tokens": 27,
        "reasoning_output_tokens": 7,
    }

    usage = usage_from_events([normalized, duplicate, second_message], "opencode-model")
    assert len(usage) == 1
    assert usage[0].input_tokens == 144
    assert usage[0].cached_input_tokens == 40
    assert usage[0].cache_read_input_tokens == 33
    assert usage[0].cache_creation_input_tokens == 7
    assert usage[0].output_tokens == 30
    assert usage[0].reasoning_output_tokens == 8


def test_unknown_event_is_preserved() -> None:
    event = {"type": "future_event", "part": {"value": 1}}

    assert normalize_opencode_event(event) == event
