"""Opt-in Claude Code acceptance smoke for plugin and MCP isolation."""
import json
import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from test_wsl2_llm.cli import app

pytestmark = [pytest.mark.live, pytest.mark.wsl_acceptance]


@pytest.mark.timeout(900)
def test_claude_plugin_and_mcp_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run only when the caller supplies a real Claude fixture environment."""
    if os.environ.get("TEST_WSL2_LLM_CLAUDE_SMOKE") != "1":
        pytest.skip("set TEST_WSL2_LLM_CLAUDE_SMOKE=1 for the opt-in Claude smoke")
    marketplace = os.environ.get("TEST_WSL2_LLM_CLAUDE_MARKETPLACE")
    plugin = os.environ.get("TEST_WSL2_LLM_CLAUDE_PLUGIN")
    server_command = os.environ.get("TEST_WSL2_LLM_CLAUDE_MCP_COMMAND")
    server_name = os.environ.get("TEST_WSL2_LLM_CLAUDE_MCP_NAME", "smoke-server")
    model = os.environ.get("TEST_WSL2_LLM_CLAUDE_MODEL", "claude-sonnet")
    distro = os.environ.get("TEST_WSL2_LLM_CLAUDE_DISTRO")
    if not marketplace or not plugin or not server_command:
        pytest.skip(
            "set TEST_WSL2_LLM_CLAUDE_MARKETPLACE, "
            "TEST_WSL2_LLM_CLAUDE_PLUGIN, and TEST_WSL2_LLM_CLAUDE_MCP_COMMAND"
        )
    source = tmp_path / ".claude.json"
    source.write_text(
        json.dumps({"mcpServers": {server_name: {"command": server_command}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    output = tmp_path / "claude-smoke"
    args = [
        "run", "--agent", "claude", "--model", model,
        "--marketplace", marketplace, "--plugin", plugin, "--mcp", server_name,
        "--prompt",
        "Use the installed plugin and call the MCP smoke tool once. Reply with CLAUDE_SMOKE_OK.",
        "--output", str(output),
    ]
    if distro:
        args[1:1] = ["--distro", distro]
    result = CliRunner().invoke(app, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    data = yaml.safe_load(output.with_suffix(".yaml").read_text(encoding="utf-8"))
    assert data["skills"]["marketplaces"] == [str(Path(marketplace).resolve())]
    assert data["skills"]["plugins"] == [plugin]
    assert data["configuration"]["mcp_servers"] == [server_name]
    assert "CLAUDE_SMOKE_OK" in (data["result"]["final_message"] or "")