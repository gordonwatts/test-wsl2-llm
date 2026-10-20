"""Normalize OpenCode's newline-delimited JSON events for shared trace handling."""

import math
from typing import Any


def normalize_opencode_event(value: dict[str, object]) -> dict[str, object]:
    """Map OpenCode v1 events to the common event shapes used by the runner.

    Unknown events pass through unchanged. For events whose type is translated,
    ``event_type`` retains OpenCode's original type and the original payload is
    otherwise preserved for diagnostics.
    """
    event_type = value.get("type")
    if not isinstance(event_type, str):
        return dict(value)

    if event_type == "text":
        part = value.get("part")
        text = part.get("text") if isinstance(part, dict) else None
        if not isinstance(text, str):
            return dict(value)
        return {
            **value,
            "type": "item.completed",
            "event_type": event_type,
            "item": {"type": "agent_message", "text": text},
        }

    if event_type == "tool_use":
        part = value.get("part")
        if not isinstance(part, dict):
            return dict(value)
        return {
            **value,
            "type": "item.completed",
            "event_type": event_type,
            "item": {
                "type": "tool_call",
                "tool": part.get("tool"),
                "state": part.get("state"),
            },
        }

    if event_type == "step_finish":
        part = value.get("part")
        part = part if isinstance(part, dict) else {}
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        input_tokens = _nonnegative_int(tokens.get("input"))
        cache_read = _nonnegative_int(cache.get("read"))
        cache_write = _nonnegative_int(cache.get("write"))
        usage: dict[str, object] = {
            "input_tokens": input_tokens + cache_read + cache_write,
            "cached_input_tokens": cache_read + cache_write,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            # OpenCode stores non-reasoning output and reasoning separately;
            # the harness prices total generated tokens as output.
            "output_tokens": (
                _nonnegative_int(tokens.get("output"))
                + _nonnegative_int(tokens.get("reasoning"))
            ),
            "reasoning_output_tokens": _nonnegative_int(tokens.get("reasoning")),
        }
        message_id = part.get("messageID")
        normalized: dict[str, object] = {
            **value,
            "type": "turn.completed",
            "event_type": event_type,
            "usage": usage,
            "usage_source": "opencode_step_finish",
        }
        cost = part.get("cost")
        if (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
        ):
            normalized["usage_cost"] = float(cost)
        if isinstance(message_id, str):
            normalized["usage_message_id"] = message_id
        return normalized

    if event_type == "step_start":
        return {**value, "type": "turn.started", "event_type": event_type}

    return dict(value)


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0
