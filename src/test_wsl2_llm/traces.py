"""Parse Codex JSONL, usage, final messages, and timing evidence."""

import json
from collections.abc import Iterator
from typing import Any

from test_wsl2_llm.models import TimingField, TraceEvent, UsageRecord

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
