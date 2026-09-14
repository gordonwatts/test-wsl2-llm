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

    def preflight(self, target: ExecutionTarget) -> AgentPreflight: ...

    def command(
        self,
        target: ExecutionTarget,
        *,
        home: str,
        model: str,
        reasoning_effort: str,
        workspace: str,
    ) -> list[str]: ...

    def normalize_event(self, value: dict[str, object]) -> dict[str, object]: ...


class CodexAgentAdapter:
    """Adapter for the existing Codex CLI behavior."""

    name = "codex"
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
    ) -> list[str]:
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
    ) -> list[str]:
        del home, model, reasoning_effort, workspace
        event = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "fake agent response"},
            }
        )
        return target.command(target.shell_command('printf "%s\\n" "$1"', event))

    def normalize_event(self, value: dict[str, object]) -> dict[str, object]:
        return value


_ADAPTERS: dict[str, type[CodexAgentAdapter] | type[FakeAgentAdapter]] = {
    "codex": CodexAgentAdapter,
    "fake": FakeAgentAdapter,
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
    "FakeAgentAdapter",
    "get_agent_adapter",
    "validate_agent_capabilities",
]
