"""Parse Codex JSONL, usage, final messages, and timing evidence."""

from __future__ import annotations

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
            "output_tokens",
            "reasoning_output_tokens",
        )
    }
    found = False
    for event in events:
        if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), dict):
            continue
        found = True
        for key in totals:
            value = event["usage"].get(key, 0)
            if isinstance(value, int):
                totals[key] += value
    if not found:
        return []
    return [
        UsageRecord(
            model=model,
            attribution="inferred - model not directly reported",
            **totals,
        )
    ]


def final_message_from_events(events: list[dict[str, Any]]) -> str | None:
    messages: list[str] = []
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(item["text"])
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
