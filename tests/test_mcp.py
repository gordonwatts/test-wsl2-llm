import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from test_report import sample_result
from typer.testing import CliRunner

from test_wsl2_llm.cli import app
from test_wsl2_llm.config import build_config, load_config_file, save_config
from test_wsl2_llm.models import TestConfig as RunConfig
from test_wsl2_llm.runner import _codex_config, _load_mcp_servers, continue_test, run_test


def local_config(monkeypatch, tmp_path, content):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(content, encoding="utf-8")


def test_import_preserves_complete_selected_tables(monkeypatch, tmp_path):
    content = """
model = "unrelated-local-model"
[mcp_servers."server.with.dots"]
command = "uvx"
args = ["--from", "package", "serve"]
cwd = "/srv/mcp"
enabled = false
startup_timeout_sec = 17.5
enabled_tools = ["read"]
future_flag = true
[mcp_servers."server.with.dots".env]
TOKEN = "fake-secret"
[mcp_servers.web]
url = "https://example.invalid/mcp"
bearer_token_env_var = "SERVICE_TOKEN"
http_headers = { X-Custom = "value" }
[mcp_servers.unselected]
command = "omit-me"
"""
    local_config(monkeypatch, tmp_path, content)
    config = RunConfig(
        prompt="/mcp", model="gpt-test", output="out", mcp_servers=["server.with.dots", "web"]
    )
    rendered = tomllib.loads(_codex_config(config))
    expected = tomllib.loads(content)["mcp_servers"]
    assert rendered["mcp_servers"] == {name: expected[name] for name in config.mcp_servers}
    assert rendered["model"] == "gpt-test"
    assert "fake-secret" not in config.model_dump_json()


def test_default_local_codex_home(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('[mcp_servers.test]\ncommand="server"')
    assert _load_mcp_servers(["test"]) == {"test": {"command": "server"}}


def test_no_servers_does_not_require_local_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert _load_mcp_servers([]) == {}


@pytest.mark.parametrize("content", ["", 'mcp_servers = "invalid"'])
def test_missing_name_reports_name_and_path(monkeypatch, tmp_path, content):
    local_config(monkeypatch, tmp_path, content)
    with pytest.raises(ValueError, match="MCP server 'missing'.*config.toml"):
        _load_mcp_servers(["missing"])


@pytest.mark.parametrize(
    "content", [None, 'secret = "unterminated-secret', "mcp_servers = { x = 1 }"]
)
def test_unreadable_or_invalid_config_reports_safe_error(monkeypatch, tmp_path, content):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    if content is not None:
        local_config(monkeypatch, tmp_path, content)
    with pytest.raises(ValueError, match="config.toml") as error:
        _load_mcp_servers(["x"])
    assert "unterminated-secret" not in str(error.value)


def test_names_round_trip_with_cli_precedence(tmp_path):
    config = build_config(
        {"mcp_servers": ["yaml"]},
        {"prompt": "hello", "model": "gpt-test", "mcp_servers": ["cli", "cli"]},
    )
    assert config.mcp_servers == ["cli"]
    destination = tmp_path / "saved.yaml"
    save_config(config, destination)
    assert build_config(load_config_file(destination), {}).mcp_servers == ["cli"]
    with pytest.raises(ValueError, match="must not be empty"):
        config.model_validate({**config.model_dump(), "mcp_servers": [" "]})


@pytest.mark.parametrize("command", ["run", "template", "continue"])
def test_cli_passes_server_names_without_copying_configuration(command, monkeypatch, tmp_path):
    destination = tmp_path / "saved.yaml"
    args = ["--mcp", "one", "--mcp", "two", "--save-config", str(destination), "--config-only"]
    if command == "run":
        args = ["run", "--prompt", "hello", "--model", "gpt-test", *args]
    elif command == "template":
        source = tmp_path / "template.yaml"
        source.write_text(
            yaml.safe_dump(
                {
                    "model": "gpt-test",
                    "prompt_template": "{{ question }}",
                    "questions": [{"id": "q", "question": "hello"}],
                }
            )
        )
        args = ["template", "run", str(source), *args]
    else:
        previous = sample_result()
        previous.configuration.update(mcp_servers=["inherited"], prompt=previous.prompt)
        source = tmp_path / "previous.yaml"
        source.write_text(yaml.safe_dump(previous.model_dump(mode="json")))
        args = ["continue", str(source), "--prompt", "more", *args]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    saved = yaml.safe_load(destination.read_text())
    assert saved["mcp_servers"] == (["inherited"] if command == "continue" else []) + ["one", "two"]


@pytest.mark.parametrize("continuation", [False, True])
def test_runner_writes_selected_servers_to_isolated_config(continuation, monkeypatch, tmp_path):
    import test_wsl2_llm.runner as module

    local_config(monkeypatch, tmp_path, '[mcp_servers.demo]\ncommand="server"\n')
    writes = {}

    def fake_bash(self, script, *args, **kwargs):
        return subprocess.CompletedProcess([], 0, b"/tmp/test-mcp", b"")

    def record_write(_client, path, content):
        writes[path] = content
        if path.endswith("/config.toml"):
            raise RuntimeError("stop after writing isolated config")

    monkeypatch.setattr(module.WslClient, "bash", fake_bash)
    monkeypatch.setattr(module.WslClient, "login_bash", fake_bash)
    monkeypatch.setattr(module, "_write_wsl_file", record_write)
    monkeypatch.setattr(module, "_inventory", lambda *_: [])
    monkeypatch.setattr(module, "_session_traces", lambda *_: ([], []))
    config = RunConfig(
        prompt="/mcp", model="gpt-test", output=str(tmp_path / "out"), mcp_servers=["demo"]
    )
    result = continue_test(sample_result(), config, "/mcp") if continuation else run_test(config)
    assert "stop after writing" in result.run.error
    remote_path = next(path for path in writes if path.endswith("/config.toml"))
    assert "/.harness/codex-home/config.toml" in remote_path
    assert tomllib.loads(writes[remote_path])["mcp_servers"] == {"demo": {"command": "server"}}
    assert result.configuration["mcp_servers"] == ["demo"]


def test_missing_server_fails_before_wsl(monkeypatch, tmp_path):
    local_config(monkeypatch, tmp_path, "")

    def unexpected(*_args, **_kwargs):
        pytest.fail("WSL must not be invoked for an unknown server")

    monkeypatch.setattr("test_wsl2_llm.runner.WslClient.run", unexpected)
    result = run_test(
        RunConfig(prompt="/mcp", model="gpt-test", output="out", mcp_servers=["oops"])
    )
    assert result.run.status == "failed"
    assert "MCP server 'oops'" in result.run.error
    assert result.run.workspace_path is None
