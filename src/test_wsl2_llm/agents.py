"""Agent adapters and capability validation.

The harness owns workspace and target lifecycle.  Agent adapters only describe
what an agent can do, perform agent-specific preflight, construct its command,
and normalize its event stream.  Keeping this boundary explicit prevents a
new agent from accidentally inheriting Codex-only options.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from test_wsl2_llm.models import TestConfig

if TYPE_CHECKING:
    from test_wsl2_llm.target import ExecutionTarget


@dataclass(frozen=True)
class AgentCapabilities:
    """Capabilities that must be checked before a workspace is created."""

    plugins: bool = False
    mcp: bool = False
    reasoning_efforts: frozenset[str] = frozenset()
    interactive_follow_up: bool = False
    requires_auth: bool = True


@dataclass(frozen=True)
class AgentPreflight:
    """Agent identity discovered during preflight."""

    version: str | None


class AgentAdapter(Protocol):
    """Narrow agent-specific contract used by the generic runner."""

    name: str
    capabilities: AgentCapabilities
    home_name: str
    auth_filename: str

    def preflight(self, target: ExecutionTarget) -> AgentPreflight: ...

    def command(
        self,
        target: ExecutionTarget,
        *,
        home: str,
        model: str,
        reasoning_effort: str,
        workspace: str,
        mcp_config: str | None = None,
    ) -> list[str]: ...

    def normalize_event(self, value: dict[str, object]) -> dict[str, object]: ...


class CodexAgentAdapter:
    """Adapter for the existing Codex CLI behavior."""

    name = "codex"
    home_name = "codex-home"
    auth_filename = "auth.json"
    capabilities = AgentCapabilities(
        plugins=True,
        mcp=True,
        reasoning_efforts=frozenset({"minimal", "low", "medium", "high", "xhigh"}),
        interactive_follow_up=True,
        requires_auth=True,
    )

    def preflight(self, target: ExecutionTarget) -> AgentPreflight:
        version = target.text(target.login_bash("codex --version")).strip()
        # This check is intentionally retained from the Codex runner: plugin
        # installation should fail during preflight, before workspace creation.
        target.login_bash("codex plugin --help")
        return AgentPreflight(version=version)

    def command(
        self,
        target: ExecutionTarget,
        *,
        home: str,
        model: str,
        reasoning_effort: str,
        workspace: str,
        mcp_config: str | None = None,
    ) -> list[str]:
        del mcp_config
        return target.command(
            target.shell_command(
                'exec env CODEX_HOME="$1" codex exec --json --skip-git-repo-check '
                '--model "$2" --config "$3" --cd "$4" -',
                home,
                model,
                f'model_reasoning_effort="{reasoning_effort}"',
                workspace,
                interactive_login=True,
            )
        )

    def normalize_event(self, value: dict[str, object]) -> dict[str, object]:
        return value


class FakeAgentAdapter:
    """Deterministic second adapter used to exercise dispatch without credentials."""

    name = "fake"
    home_name = "fake-home"
    auth_filename = "auth.json"
    capabilities = AgentCapabilities(
        plugins=False,
        mcp=False,
        reasoning_efforts=frozenset({"minimal", "low", "medium", "high", "xhigh"}),
        interactive_follow_up=False,
        requires_auth=False,
    )

    def preflight(self, target: ExecutionTarget) -> AgentPreflight:
        del target
        return AgentPreflight(version="fake-agent 1.0")

    def command(
        self,
        target: ExecutionTarget,
        *,
        home: str,
        model: str,
        reasoning_effort: str,
        workspace: str,
        mcp_config: str | None = None,
    ) -> list[str]:
        del home, model, reasoning_effort, workspace, mcp_config
        event = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "fake agent response"},
            }
        )
        return target.command(target.shell_command('printf "%s\\n" "$1"', event))

    def normalize_event(self, value: dict[str, object]) -> dict[str, object]:
        return value


class ClaudeCodeAgentAdapter:
    """Adapter for one noninteractive Claude Code ``--print`` run."""

    name = "claude"
    home_name = "claude-home"
    auth_filename = ".credentials.json"
    capabilities = AgentCapabilities(
        plugins=True,
        mcp=True,
        # Claude model selection is independent of Codex reasoning effort. The
        # compatibility value is accepted but never forwarded to Claude.
        reasoning_efforts=frozenset({"minimal", "low", "medium", "high", "xhigh"}),
        interactive_follow_up=False,
        requires_auth=False,
    )

    def preflight(self, target: ExecutionTarget) -> AgentPreflight:
        version = target.text(target.login_bash("claude --version")).strip()
        return AgentPreflight(version=version)

    def command(
        self,
        target: ExecutionTarget,
        *,
        home: str,
        model: str,
        reasoning_effort: str,
        workspace: str,
        mcp_config: str | None = None,
    ) -> list[str]:
        del reasoning_effort
        return target.command(
            target.shell_command(
                'cd -- "$3" && args=(--print --output-format stream-json --verbose '
                '--model "$2" --permission-mode bypassPermissions); '
                'if test -n "$4"; then args+=(--mcp-config "$4"); fi; '
                'exec env CLAUDE_CONFIG_DIR="$1" claude "${args[@]}"',
                home,
                model,
                workspace,
                mcp_config or "",
                interactive_login=True,
            )
        )

    def normalize_event(self, value: dict[str, object]) -> dict[str, object]:
        event_type = value.get("type")
        if event_type == "assistant":
            message = value.get("message")
            normalized = dict(value)
            normalized["type"] = "item.completed"
            text = _claude_text(message.get("content")) if isinstance(message, dict) else ""
            normalized["item"] = {"type": "agent_message", "text": text}
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                normalized["usage"] = _claude_usage(message["usage"])
            return normalized
        if event_type == "result":
            normalized = dict(value)
            normalized["type"] = "turn.completed"
            result = value.get("result")
            if isinstance(result, str):
                normalized["item"] = {"type": "agent_message", "text": result}
            if isinstance(value.get("usage"), dict):
                normalized["usage"] = _claude_usage(value["usage"])
            return normalized
        if event_type == "system":
            normalized = dict(value)
            normalized["type"] = "session.started"
            return normalized
        # Retain unknown events for diagnostics; shared extractors ignore them.
        return value


def _claude_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _claude_usage(value: dict[str, object]) -> dict[str, int]:
    """Map Claude usage names onto the runner's canonical token fields."""
    def integer(*names: str) -> int:
        for name in names:
            item = value.get(name)
            if isinstance(item, int):
                return item
        return 0

    return {
        "input_tokens": integer("input_tokens"),
        "cached_input_tokens": integer(
            "cached_input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"
        ),
        "output_tokens": integer("output_tokens"),
        "reasoning_output_tokens": integer("reasoning_output_tokens"),
    }


_ADAPTERS: dict[str, type[AgentAdapter]] = {
    "codex": CodexAgentAdapter,
    "fake": FakeAgentAdapter,
    "claude": ClaudeCodeAgentAdapter,
    "claude-code": ClaudeCodeAgentAdapter,
}


def get_agent_adapter(name: str) -> AgentAdapter:
    """Return a fresh adapter, rejecting unknown agents deterministically."""
    try:
        return _ADAPTERS[name.casefold()]()
    except KeyError:
        available = ", ".join(sorted(_ADAPTERS))
        raise ValueError(f"unknown agent '{name}'; available agents: {available}") from None


def validate_agent_capabilities(
    config: TestConfig, *, interactive_follow_up: bool = False
) -> AgentAdapter:
    """Validate all requested options before target/workspace side effects."""
    adapter = get_agent_adapter(config.agent)
    capabilities = adapter.capabilities
    unsupported: list[str] = []
    if config.plugins and not capabilities.plugins:
        unsupported.append("plugins")
    if config.marketplaces and not capabilities.plugins:
        unsupported.append("marketplaces")
    if config.mcp_servers and not capabilities.mcp:
        unsupported.append("MCP servers")
    if config.reasoning_effort not in capabilities.reasoning_efforts:
        unsupported.append(f"reasoning effort '{config.reasoning_effort}'")
    if interactive_follow_up and not capabilities.interactive_follow_up:
        unsupported.append("interactive follow-up")
    if unsupported:
        raise ValueError(
            f"agent '{adapter.name}' does not support: {', '.join(unsupported)}"
        )
    return adapter


__all__ = [
    "AgentAdapter",
    "AgentCapabilities",
    "AgentPreflight",
    "CodexAgentAdapter",
    "ClaudeCodeAgentAdapter",
    "FakeAgentAdapter",
    "get_agent_adapter",
    "validate_agent_capabilities",
]
