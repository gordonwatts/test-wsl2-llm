"""Parse Codex JSONL, usage, final messages, and timing evidence."""

import json
from collections.abc import Iterator
from typing import Any

from test_wsl2_llm.models import SessionTrace, TimingField, TraceEvent, UsageRecord

TIMING_WORDS = (
    "timestamp",
    "started_at",
    "finished_at",
    "completed_at",
    "duration",
    "elapsed",
    "latency",
)


def parse_json_line(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def extract_timing_fields(value: Any, prefix: str = "$") -> list[TimingField]:
    fields: list[TimingField] = []
    for path, key, item in _walk(value, prefix):
        lowered = key.lower()
        if any(word in lowered for word in TIMING_WORDS) and isinstance(item, (str, int, float)):
            fields.append(
                TimingField(
                    path=path,
                    value=item,
                    normalized_seconds=_normalize_duration(lowered, item),
                )
            )
    return fields


def trace_event_from_json(
    value: dict[str, Any],
    *,
    source: str,
    sequence: int,
    stream: str | None = None,
    received_at: str | None = None,
    elapsed_seconds: float | None = None,
) -> TraceEvent:
    event_type = value.get("type")
    return TraceEvent(
        source=source,
        sequence=sequence,
        stream=stream,
        event_type=event_type if isinstance(event_type, str) else None,
        received_at=received_at,
        elapsed_seconds=elapsed_seconds,
        timing_fields=extract_timing_fields(value),
    )


def usage_from_events(events: list[dict[str, Any]], model: str) -> list[UsageRecord]:
    totals = {
        key: 0
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_creation_5m_input_tokens",
            "cache_creation_1h_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
    }
    found = False
    completed_events = [
        event
        for event in events
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict)
    ]
    claude_messages = [
        event
        for event in events
        if event.get("usage_source") == "claude_assistant_message"
        and isinstance(event.get("usage"), dict)
        and any(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in event["usage"].values()
        )
    ]
    # Claude's result event may omit usage altogether. Its assistant message
    # events contain per-API-message usage, so use those as the complete source
    # when present; otherwise retain aggregate turn usage (as in Codex).
    if claude_messages:
        selected_events = []
        seen_message_ids: set[str] = set()
        for event in claude_messages:
            message_id = event.get("usage_message_id")
            if isinstance(message_id, str):
                if message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
            selected_events.append(event)
    else:
        selected_events = completed_events

    model_totals: dict[str, dict[str, int]] = {}
    for event in selected_events:
        if not isinstance(event.get("usage"), dict):
            continue
        usage = event["usage"]
        event_model = event.get("usage_model")
        usage_model = event_model if isinstance(event_model, str) else model
        event_totals = model_totals.setdefault(
            usage_model, {key: 0 for key in totals}
        )
        aliases = {
            "input_tokens": ("input_tokens",),
            "cached_input_tokens": ("cached_input_tokens",),
            "cache_read_input_tokens": ("cache_read_input_tokens",),
            "cache_creation_input_tokens": ("cache_creation_input_tokens",),
            "cache_creation_5m_input_tokens": ("cache_creation_5m_input_tokens",),
            "cache_creation_1h_input_tokens": ("cache_creation_1h_input_tokens",),
            "output_tokens": ("output_tokens",),
            "reasoning_output_tokens": ("reasoning_output_tokens",),
        }
        event_found = False
        for key, names in aliases.items():
            values = [usage.get(name) for name in names]
            integers = [value for value in values if isinstance(value, int)]
            if integers:
                event_found = True
                event_totals[key] += sum(integers)
        # Codex may report a single cached-input count, while Claude reports
        # cache reads and writes separately. Preserve both shapes without
        # counting detailed values twice when the aggregate is also present.
        cached_total = usage.get("cached_input_tokens")
        if not isinstance(cached_total, int):
            cached_total = sum(
                int(usage.get(name, 0))
                for name in ("cache_read_input_tokens", "cache_creation_input_tokens")
                if isinstance(usage.get(name), int)
            )
            event_totals["cached_input_tokens"] += cached_total
        if any(
            isinstance(usage.get(name), int)
            for name in (
                "cached_input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        ):
            event_found = True
        found = found or event_found
    if not found:
        return []
    return [
        UsageRecord(
            model=usage_model,
            attribution="inferred - model not directly reported",
            **usage_totals,
        )
        for usage_model, usage_totals in model_totals.items()
    ]


def codex_subagent_usage_from_traces(
    events: list[dict[str, Any]],
    session_traces: list[SessionTrace],
    previous_session_traces: list[SessionTrace] | None = None,
) -> list[UsageRecord]:
    """Price child threads from Codex session traces, excluding parent stdout usage.

    Session token counts are cumulative. A continuation keeps old trace files, so
    only positive increments after the previous snapshot belong to this run.
    """
    root_id = next(
        (
            event.get("thread_id")
            for event in events
            if event.get("type") == "thread.started"
            and isinstance(event.get("thread_id"), str)
        ),
        None,
    )
    if root_id is None:
        return []

    prior = {trace.path: trace.content for trace in previous_session_traces or []}
    children: dict[str, tuple[str, list[tuple[int, dict[str, Any]]], int]] = {}
    for trace in session_traces:
        rows = [
            (index, parsed)
            for index, line in enumerate(trace.content.splitlines())
            if (parsed := parse_json_line(line))
        ]
        metadata = next(
            (row.get("payload") for _, row in rows if row.get("type") == "session_meta"), None
        )
        if not isinstance(metadata, dict) or not isinstance(metadata.get("id"), str):
            continue
        source = metadata.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        parent_id = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
        if not isinstance(parent_id, str):
            continue
        old_content = prior.get(trace.path, "")
        # A replaced or truncated file cannot be safely compared with the old
        # snapshot; count it as a new trace rather than dropping its usage.
        old_lines = len(old_content.splitlines()) if trace.content.startswith(old_content) else 0
        children[metadata["id"]] = (parent_id, rows, old_lines)

    descendants = {root_id}
    pending = dict(children)
    while pending:
        found = {
            thread_id for thread_id, (parent_id, _, _) in pending.items()
            if parent_id in descendants
        }
        if not found:
            break
        descendants.update(found)
        for thread_id in found:
            pending.pop(thread_id)

    totals: dict[str, dict[str, int]] = {}
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    for thread_id in sorted(descendants - {root_id}):
        _, rows, old_lines = children[thread_id]
        previous_total = {field: 0 for field in fields}
        active_model: str | None = None
        for index, row in rows:
            payload = row.get("payload")
            if row.get("type") == "turn_context" and isinstance(payload, dict):
                candidate = payload.get("model")
                if isinstance(candidate, str) and candidate:
                    active_model = candidate
            if row.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            info = payload.get("info")
            if payload.get("type") != "token_count" or not isinstance(info, dict):
                continue
            usage = info.get("total_token_usage")
            if not isinstance(usage, dict):
                continue
            current = {}
            for field in fields:
                value = usage.get(field)
                current[field] = (
                    value if isinstance(value, int) and not isinstance(value, bool) else 0
                )
            if index >= old_lines and active_model:
                model_totals = totals.setdefault(active_model, {field: 0 for field in fields})
                for field in fields:
                    delta = current[field] - previous_total[field]
                    model_totals[field] += delta if delta >= 0 else current[field]
            previous_total = current

    return [
        UsageRecord(model=model, attribution="reported in Codex subagent trace", **counts)
        for model, counts in sorted(totals.items())
        if any(counts.values())
    ]


def final_message_from_events(events: list[dict[str, Any]]) -> str | None:
    messages: list[str] = []
    for event in events:
        item = event.get("item")
        if (
            event.get("type") in {"item.completed", "turn.completed"}
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(item["text"])
        elif event.get("type") == "turn.completed" and isinstance(event.get("result"), str):
            messages.append(event["result"])
    return messages[-1] if messages else None


def _walk(value: Any, prefix: str) -> Iterator[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            yield path, str(key), item
            yield from _walk(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{prefix}[{index}]")


def _normalize_duration(key: str, value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    if key.endswith("_ms") or "milliseconds" in key:
        return float(value) / 1000
    if key.endswith("_seconds") or key.endswith("_secs") or key.endswith("_sec"):
        return float(value)
    return None
