from pathlib import Path
from subprocess import CompletedProcess

import pytest

from test_wsl2_llm.agents import (
    ClaudeCodeAgentAdapter,
    CodexAgentAdapter,
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


def test_codex_setup_commands_each_wait_before_dispatch() -> None:
    from test_wsl2_llm.runner import _check_mcp_servers

    events: list[str] = []

    class Target:
        def login_bash(self, script, *_args, **_kwargs):
            assert events[-1] == "gate"
            events.append(script)
            return CompletedProcess([], 0, stdout=b"[]")

        def text(self, completed):
            return completed.stdout.decode()

    target = Target()
    adapter = CodexAgentAdapter()
    def gate() -> None:
        events.append("gate")

    adapter.preflight(target, before_launch=gate)
    adapter.install_plugins(target, "home", ["marketplace"], ["plugin"], before_launch=gate)
    _check_mcp_servers(target, "home", [], before_launch=gate)
    assert len(events) == 10
    assert events[::2] == ["gate"] * 5


def test_codex_native_auth_uses_resolved_source_and_canonical_filename(tmp_path: Path) -> None:
    from test_wsl2_llm.agents import CodexAgentAdapter
    from test_wsl2_llm.runner import LinuxClient

    source = tmp_path / "source credentials.json"
    source.write_text('{"synthetic":true}', encoding="utf-8")
    home = tmp_path / "isolated home"
    target = LinuxClient()
    adapter = CodexAgentAdapter()

    resolved = adapter.resolve_auth_source(target, str(source))
    adapter.setup_home(target, str(home), resolved)

    assert (home / "auth.json").read_bytes() == source.read_bytes()
    assert source.exists()


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


def test_codex_command_uses_run_owned_temp_directory(tmp_path: Path) -> None:
    """Execute the generated launcher, including paths requiring shell quoting."""
    import os
    import subprocess

    if os.name == "nt":
        pytest.skip("Generated POSIX launcher requires bash")
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    executable.write_text('#!/bin/sh\ntest -d "$TMPDIR" && printf "%s" "$TMPDIR"\n')
    executable.chmod(0o700)
    home = tmp_path / "run home; literal" / "codex-home"
    workspace = tmp_path / "workspace; literal"
    # Avoid login startup files replacing PATH; execute the adapter's shell body.
    target = CommandTarget()
    captured = CodexAgentAdapter().command(
        target, home=str(home), model="model", reasoning_effort="medium",
        workspace=str(workspace),
    )
    completed = subprocess.run(
        ["bash", "-c", captured[0], "launcher", *captured[1:]],
        env={**os.environ, "PATH": f"{executable.parent}:{os.environ['PATH']}"},
        capture_output=True, text=True, check=True,
    )
    assert completed.stdout == str(home.parent / "tmp")


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
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
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
