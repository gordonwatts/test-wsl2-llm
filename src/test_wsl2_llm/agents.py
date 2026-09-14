"""Agent adapters and capability validation.

The harness owns workspace and target lifecycle.  Agent adapters only describe
what an agent can do, perform agent-specific preflight, construct its command,
and normalize its event stream.  Keeping this boundary explicit prevents a
new agent from accidentally inheriting Codex-only options.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
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


@dataclass(frozen=True)
class AgentPluginSetup:
    """Agent-specific plugin installation evidence."""

    installed_roots: tuple[str, ...] = ()
    plugin_versions: dict[str, str] = field(default_factory=dict)


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
    def configuration(self, config: TestConfig) -> str: ...
    def resolve_auth_source(self, target: ExecutionTarget, source: str | None) -> str: ...
    def setup_home(self, target: ExecutionTarget, home: str, auth_source: str) -> None: ...
    def mcp_config(self, config: TestConfig) -> dict[str, object] | None: ...
    def install_plugins(
        self, target: ExecutionTarget, home: str, marketplaces: list[str], plugins: list[str]
    ) -> AgentPluginSetup: ...
    def result_version(self, version: str | None) -> str | None: ...


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

    def configuration(self, config: TestConfig) -> str:
        from test_wsl2_llm.runner import _codex_config

        return _codex_config(config)

    def resolve_auth_source(self, target: ExecutionTarget, source: str | None) -> str:
        from test_wsl2_llm.runner import _resolve_wsl_path

        return _resolve_wsl_path(target, source or "~/.codex/auth.json")

    def setup_home(self, target: ExecutionTarget, home: str, auth_source: str) -> None:
        target.bash(
            'mkdir -p "$1" && cp -- "$2" "$1/auth.json" && chmod 600 "$1/auth.json"',
            home,
            auth_source,
        )

    def mcp_config(self, config: TestConfig) -> dict[str, object] | None:
        del config
        return None

    def install_plugins(
        self, target: ExecutionTarget, home: str, marketplaces: list[str], plugins: list[str]
    ) -> AgentPluginSetup:
        from test_wsl2_llm.runner import _installed_paths_from_json, _plugin_manifest_version

        roots: list[str] = []
        versions: dict[str, str] = {}
        for source in marketplaces:
            target.login_bash(
                'env CODEX_HOME="$1" codex plugin marketplace add "$2" --json', home, source
            )
        for plugin in plugins:
            installed = target.login_bash(
                'env CODEX_HOME="$1" codex plugin add "$2" --json', home, plugin
            )
            plugin_roots = _installed_paths_from_json(target.text(installed))
            roots.extend(plugin_roots)
            for root in plugin_roots:
                version = _plugin_manifest_version(target, root)
                if version is not None:
                    versions[plugin] = version
        return AgentPluginSetup(tuple(roots), versions)

    def result_version(self, version: str | None) -> str | None:
        return version

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

    def configuration(self, config: TestConfig) -> str:
        del config
        return ""

    def resolve_auth_source(self, target: ExecutionTarget, source: str | None) -> str:
        del target, source
        return ""

    def setup_home(self, target: ExecutionTarget, home: str, auth_source: str) -> None:
        del auth_source
        target.bash('mkdir -p -- "$1"', home)

    def mcp_config(self, config: TestConfig) -> dict[str, object] | None:
        del config
        return None

    def install_plugins(
        self, target: ExecutionTarget, home: str, marketplaces: list[str], plugins: list[str]
    ) -> AgentPluginSetup:
        del target, home, marketplaces, plugins
        return AgentPluginSetup()

    def result_version(self, version: str | None) -> str | None:
        return version

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
        requires_auth=True,
    )

    def preflight(self, target: ExecutionTarget) -> AgentPreflight:
        version = target.text(target.login_bash("claude --version")).strip()
        return AgentPreflight(version=version)

    def configuration(self, config: TestConfig) -> str:
        del config
        return ""

    def resolve_auth_source(self, target: ExecutionTarget, source: str | None) -> str:
        return _resolve_claude_auth_source(source, environment=target.environment)

    def setup_home(self, target: ExecutionTarget, home: str, auth_source: str) -> None:
        target.bash('mkdir -p -- "$1"', home)
        _copy_host_auth(target, auth_source, home, self.auth_filename)

    def mcp_config(self, config: TestConfig) -> dict[str, object] | None:
        if not config.mcp_servers:
            return None
        from test_wsl2_llm.runner import _load_claude_mcp_servers

        return {"mcpServers": _load_claude_mcp_servers(config.mcp_servers)}

    def install_plugins(
        self, target: ExecutionTarget, home: str, marketplaces: list[str], plugins: list[str]
    ) -> AgentPluginSetup:
        for source in marketplaces:
            target.login_bash(
                'env CLAUDE_CONFIG_DIR="$1" claude plugin marketplace add "$2"', home, source
            )
        for plugin in plugins:
            target.login_bash(
                'env CLAUDE_CONFIG_DIR="$1" claude plugin install "$2" --scope user --yes',
                home,
                plugin,
            )
        return AgentPluginSetup()

    def result_version(self, version: str | None) -> str | None:
        return version

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


def _resolve_claude_auth_source(source: str | None, *, environment: Mapping[str, str]) -> str:
    """Resolve Claude credentials on the host that invoked the harness."""
    candidates: list[Path] = []
    if source and source.strip():
        candidates.append(_expand_host_path(source, environment))
    else:
        config_dir = environment.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            candidates.append(_expand_host_path(config_dir, environment) / ".credentials.json")
        for variable in ("USERPROFILE", "HOME"):
            home = environment.get(variable)
            if home:
                candidates.append(Path(home) / ".claude" / ".credentials.json")
        if not candidates:
            candidates.append(Path.home() / ".claude" / ".credentials.json")

    checked: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            resolved = candidate.expanduser()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        checked.append(str(resolved))
        if resolved.is_file():
            return str(resolved)

    locations = ", ".join(checked) if checked else "the invocation environment"
    if source and source.strip():
        raise FileNotFoundError(
            f"Claude credentials were not found at {locations}. "
            "Provide a readable --auth-source PATH."
        )
    raise FileNotFoundError(
        f"Claude credentials were not found in {locations}. "
        "Expected %USERPROFILE%\\.claude\\.credentials.json on Windows; "
        "provide --auth-source PATH to override."
    )


def _expand_host_path(value: str, environment: Mapping[str, str]) -> Path:
    """Expand ``~`` using the supplied invocation environment, not WSL state."""
    if value == "~" or value.startswith("~/") or value.startswith("~\\"):
        home = environment.get("USERPROFILE") or environment.get("HOME")
        if home:
            value = home + value[1:]
    return Path(value).expanduser()


def _copy_host_auth(target: ExecutionTarget, source: str, target_home: str, filename: str) -> None:
    """Copy one host credential file into an isolated target home with mode 0600."""
    copier = getattr(target, "copy_file_to_target", None)
    if callable(copier):
        copier(source, f"{target_home}/{filename}")
        return
    target.copy_to_target(source, target_home)
    source_name = Path(source).name
    if source_name != filename:
        target.bash('mv -f -- "$1/$2" "$1/$3"', target_home, source_name, filename)
    target.bash('chmod 600 -- "$1/$2"', target_home, filename)


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
        raise ValueError(f"agent '{adapter.name}' does not support: {', '.join(unsupported)}")
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
