from pathlib import Path

import pytest

from test_wsl2_llm.agents import (
    ClaudeCodeAgentAdapter,
    FakeAgentAdapter,
    get_agent_adapter,
    validate_agent_capabilities,
)
from test_wsl2_llm.models import TestConfig


def config(**overrides: object) -> TestConfig:
    values: dict[str, object] = {
        "prompt": "hello",
        "model": "gpt-test",
        "output": str(Path("result")),
    }
    values.update(overrides)
    return TestConfig.model_validate(values)


def test_fake_adapter_is_available_without_credentials() -> None:
    adapter = get_agent_adapter("fake")

    assert isinstance(adapter, FakeAgentAdapter)
    assert adapter.preflight(None).version == "fake-agent 1.0"  # type: ignore[arg-type]
    assert not adapter.capabilities.requires_auth


def test_unknown_agent_fails_deterministically() -> None:
    with pytest.raises(ValueError, match="unknown agent 'missing'"):
        get_agent_adapter("missing")


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("plugins", ["demo@market"], "plugins"),
        ("marketplaces", ["market"], "marketplaces"),
        ("mcp_servers", ["server"], "MCP servers"),
    ],
)
def test_unsupported_fake_capabilities_fail_before_execution(
    field: str, value: list[str], label: str
) -> None:
    with pytest.raises(ValueError, match=label):
        validate_agent_capabilities(config(agent="fake", **{field: value}))


def test_fake_agent_dispatches_without_live_credentials() -> None:
    adapter = validate_agent_capabilities(config(agent="fake"))

    assert adapter.name == "fake"
    assert adapter.preflight(None).version == "fake-agent 1.0"  # type: ignore[arg-type]


class CommandTarget:
    def shell_command(self, script: str, *arguments: str, **_kwargs: object) -> list[str]:
        return [script, *arguments]

    def command(self, arguments: list[str]) -> list[str]:
        return arguments


def test_fake_agent_command_is_deterministic() -> None:
    command = get_agent_adapter("fake").command(
        CommandTarget(), home="/home", model="model", reasoning_effort="medium", workspace="/work"
    )

    assert command[0] == 'printf "%s\\n" "$1"'
    assert command[1].startswith('{"type": "item.completed"')


def test_claude_adapter_builds_isolated_noninteractive_command() -> None:
    adapter = get_agent_adapter("claude")
    assert isinstance(adapter, ClaudeCodeAgentAdapter)
    assert adapter.home_name == "claude-home"
    assert adapter.auth_filename == ".credentials.json"
    assert adapter.capabilities.requires_auth
    command = adapter.command(
        CommandTarget(),
        home="/run/claude-home",
        model="claude-sonnet-4-5",
        reasoning_effort="high",
        workspace="/run/workspace",
    )
    assert "CLAUDE_CONFIG_DIR" in command[0]
    assert "--print" in command[0]
    assert "--output-format stream-json" in command[0]
    assert "--model \"$2\"" in command[0]
    assert "bypassPermissions" in command[0]
    assert "--config" not in command[0]


def test_claude_events_normalize_success_and_preserve_unknown() -> None:
    adapter = get_agent_adapter("claude")
    assistant = adapter.normalize_event(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 11, "output_tokens": 3},
            },
        }
    )
    assert assistant["type"] == "item.completed"
    assert assistant["item"] == {"type": "agent_message", "text": "hello"}
    assert assistant["usage"] == {
        "input_tokens": 11,
        "cached_input_tokens": 0,
        "output_tokens": 3,
        "reasoning_output_tokens": 0,
    }
    result = adapter.normalize_event({"type": "result", "result": "done"})
    assert result["type"] == "turn.completed"
    assert result["item"] == {"type": "agent_message", "text": "done"}
    unknown = {"type": "future_event", "payload": {"ok": True}}
    assert adapter.normalize_event(unknown) == unknown


def test_claude_accepts_plugins_and_mcp_but_rejects_follow_up() -> None:
    adapter = validate_agent_capabilities(
        config(
            agent="claude",
            plugins=["demo@marketplace"],
            marketplaces=["marketplace"],
            mcp_servers=["server"],
        )
    )
    assert adapter.name == "claude"
    with pytest.raises(ValueError, match="interactive follow-up"):
        validate_agent_capabilities(config(agent="claude"), interactive_follow_up=True)
