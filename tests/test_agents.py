from pathlib import Path

import pytest

from test_wsl2_llm.agents import (
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
