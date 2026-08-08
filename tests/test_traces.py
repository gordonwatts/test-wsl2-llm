from test_wsl2_llm.traces import (
    extract_timing_fields,
    final_message_from_events,
    parse_json_line,
    usage_from_events,
)


def test_usage_is_labeled_as_inferred_model() -> None:
    usage = usage_from_events(
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 4,
                    "output_tokens": 3,
                    "reasoning_output_tokens": 2,
                },
            }
        ],
        "gpt-test",
    )
    assert usage[0].model == "gpt-test"
    assert "not directly reported" in usage[0].attribution
    assert usage[0].input_tokens == 10


def test_timing_extraction_preserves_unknown_units() -> None:
    fields = extract_timing_fields(
        {"started_at": "2026-01-01T00:00:00Z", "latency_ms": 1500, "duration_ticks": 22}
    )
    by_path = {field.path: field for field in fields}
    assert by_path["$.latency_ms"].normalized_seconds == 1.5
    assert by_path["$.duration_ticks"].normalized_seconds is None
    assert by_path["$.started_at"].value == "2026-01-01T00:00:00Z"


def test_malformed_json_and_final_message() -> None:
    assert parse_json_line("not json") is None
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}]
    assert final_message_from_events(events) == "done"
