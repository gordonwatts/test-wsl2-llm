import subprocess

import pytest

from test_wsl2_llm.target import SshTarget


def test_command_quotes_remote_arguments_and_noninteractive_options() -> None:
    target = SshTarget("build-alias", user="runner", port=2222, connect_timeout_seconds=3.2)
    command = target.command(["printf", "%s", "a value; $HOME", ""])

    assert command[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-p", "2222"]
    assert command[7] == "runner@build-alias"
    assert "a value; $HOME" in command[-1]
    assert "StrictHostKeyChecking=no" not in command


def test_bash_preserves_positional_arguments(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, b"ok", b"")

    monkeypatch.setattr("test_wsl2_llm.target.subprocess.run", fake_run)
    target = SshTarget("host", source_environment={"PATH": "safe"})
    result = target.bash('printf %s "$1"', "space ; $HOME", input_bytes=b"prompt")

    assert result.stdout == b"ok"
    assert "space ; $HOME" in seen["command"][-1]
    assert seen["input"] == b"prompt"
    assert seen["timeout"] == 10.0
    assert seen["env"] == {"PATH": "safe"}


def test_nonzero_and_host_key_errors_are_reported(monkeypatch) -> None:
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 255, b"", b"Host key verification failed.\\n")

    monkeypatch.setattr("test_wsl2_llm.target.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="Host key verification failed"):
        SshTarget("host").run(["true"])


def test_saved_config_contains_target_metadata_only() -> None:
    target = SshTarget("host", user="runner", port=22, remote_workspace_parent="/srv/runs")
    saved = target.to_config()

    assert saved == {
        "target": "ssh",
        "host": "host",
        "user": "runner",
        "port": 22,
        "remote_workspace_parent": "/srv/runs",
        "connect_timeout_seconds": 10.0,
    }
    assert not any("password" in key or "key" in key for key in saved)
