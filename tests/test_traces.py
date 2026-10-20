import json

from test_wsl2_llm.models import SessionTrace
from test_wsl2_llm.pricing import load_and_calculate_costs
from test_wsl2_llm.traces import (
    codex_subagent_usage_from_traces,
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


def test_missing_usage_is_unavailable_not_zero() -> None:
    assert usage_from_events([{"type": "turn.completed", "usage": {}}], "claude-sonnet") == []


def test_claude_usage_aliases_are_reported() -> None:
    usage = usage_from_events(
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 4,
                    "output_tokens": 3,
                },
            }
        ],
        "claude-sonnet",
    )
    assert usage[0].input_tokens == 10
    assert usage[0].cached_input_tokens == 4
    assert usage[0].output_tokens == 3


def _session_trace(
    thread_id: str, parent_id: str, model: str, counts: list[tuple[int, int]]
) -> SessionTrace:
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id}}},
            },
        },
        {"type": "turn_context", "payload": {"model": model}},
    ]
    rows.extend(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": 0,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": output_tokens // 2,
                    }
                },
            },
        }
        for input_tokens, output_tokens in counts
    )
    return SessionTrace(
        path=f"sessions/{thread_id}.jsonl",
        content="\n".join(json.dumps(row) for row in rows) + "\n",
    )


def test_codex_subagent_usage_is_priced_separately_from_parent() -> None:
    parent_events = [
        {"type": "thread.started", "thread_id": "parent"},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
        },
    ]
    traces = [
        _session_trace("child", "parent", "gpt-6-luna", [(30, 2), (60, 5)]),
        _session_trace("grandchild", "child", "gpt-6-luna", [(15, 3)]),
        _session_trace("unrelated", "another-parent", "gpt-6-luna", [(999, 99)]),
    ]
    usage = [
        *usage_from_events(parent_events, "gpt-6.1-sol"),
        *codex_subagent_usage_from_traces(parent_events, traces),
    ]
    assert [(item.model, item.input_tokens, item.output_tokens) for item in usage] == [
        ("gpt-6.1-sol", 100, 10),
        ("gpt-6-luna", 75, 8),
    ]
    assert usage[1].reasoning_output_tokens == 3
    costs = load_and_calculate_costs(usage, None)
    assert len(costs.models) == 2
    assert costs.total_cost == sum(model.total_cost for model in costs.models)


def test_codex_subagent_continuation_counts_only_new_token_deltas() -> None:
    events = [{"type": "thread.started", "thread_id": "parent"}]
    earlier = _session_trace("child", "parent", "gpt-6-luna", [(30, 2)])
    # Preserve a malformed and a blank line to test raw trace line offsets.
    earlier.content = earlier.content + "not-json\n\n"
    new_line = _session_trace("child", "parent", "gpt-6-luna", [(50, 5)]).content.splitlines()[-1]
    later = SessionTrace(path=earlier.path, content=earlier.content + new_line + "\n")
    usage = codex_subagent_usage_from_traces(events, [later], [earlier])
    assert [(item.model, item.input_tokens, item.output_tokens) for item in usage] == [
        ("gpt-6-luna", 20, 3)
    ]
    assert codex_subagent_usage_from_traces(events, [earlier], [earlier]) == []


def test_codex_subagent_trace_ignores_malformed_metadata() -> None:
    events = [{"type": "thread.started", "thread_id": "parent"}]
    malformed = SessionTrace(
        path="sessions/bad.jsonl",
        content=json.dumps(
            {"type": "session_meta", "payload": {"id": "bad", "source": {"subagent": None}}}
        ),
    )
    assert codex_subagent_usage_from_traces(events, [malformed]) == []


def test_codex_subagent_cumulative_updates_follow_active_model() -> None:
    events = [{"type": "thread.started", "thread_id": "parent"}]
    trace = _session_trace("child", "parent", "gpt-6-luna", [(30, 2), (30, 2)])
    trace.content += "\n".join(
        json.dumps(row)
        for row in [
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 50,
                            "cached_input_tokens": 0,
                            "output_tokens": 5,
                            "reasoning_output_tokens": 2,
                        }
                    },
                },
            },
        ]
    ) + "\n"
    usage = codex_subagent_usage_from_traces(events, [trace])
    assert {item.model: (item.input_tokens, item.output_tokens) for item in usage} == {
        "gpt-6-luna": (30, 2),
        "gpt-6.1-sol": (20, 3),
    }
