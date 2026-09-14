import io
import subprocess
import tarfile
from pathlib import Path

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


def test_workspace_marker_and_owned_cleanup(monkeypatch) -> None:
    calls: list[tuple[str, bytes | None, bool]] = []
    responses = [b"/tmp/test-wsl2-llm-abcd1234\n", b"", b""]

    def fake_invoke(self, script, input_bytes, check, **kwargs):
        calls.append((script, input_bytes, check))
        return subprocess.CompletedProcess([], 0, responses.pop(0), b"")

    monkeypatch.setattr(SshTarget, "_invoke", fake_invoke)
    target = SshTarget("host")
    run_root = target.create_workspace("/tmp")
    assert run_root in target._owned_workspace_tokens
    target.cleanup_workspace(run_root)
    assert run_root not in target._owned_workspace_tokens
    assert "ownership" in calls[0][0]
    assert "rm -rf" in calls[-1][0]
    with pytest.raises(ValueError, match="not owned"):
        target.cleanup_workspace(run_root)


def test_tar_transfers_preserve_files_and_use_remote_streams(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "input $x.txt"
    source.write_text("hello", encoding="utf-8")
    captured: list[bytes] = []

    def fake_invoke(self, script, input_bytes, check, **kwargs):
        if input_bytes is not None:
            captured.append(input_bytes)
            return subprocess.CompletedProcess([], 0, b"", b"")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            tar.add(source, arcname=source.name)
        return subprocess.CompletedProcess([], 0, archive.getvalue(), b"")

    monkeypatch.setattr(SshTarget, "_invoke", fake_invoke)
    target = SshTarget("host")
    target.copy_to_target(str(source), "/tmp/work space")
    with tarfile.open(fileobj=io.BytesIO(captured[0]), mode="r:") as archive:
        assert source.name in archive.getnames()
    destination = tmp_path / "output.txt"
    target.copy_from_target("/tmp/work space/input.txt", str(destination))
    assert destination.read_text(encoding="utf-8") == "hello"


def test_cancellation_uses_owned_remote_pid(monkeypatch) -> None:
    target = SshTarget("host")
    target._owned_workspace_tokens["/tmp/test-wsl2-llm-root"] = "token"
    command = target.command(["codex", "--cd", "/tmp/test-wsl2-llm-root/workspace"])
    remote_calls: list[str] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 42
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    process = FakeProcess()
    monkeypatch.setattr("test_wsl2_llm.target.subprocess.Popen", lambda *args, **kwargs: process)

    def fake_invoke(self, script, input_bytes, check, **kwargs):
        remote_calls.append(script)
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(SshTarget, "_invoke", fake_invoke)
    target.start_process(command)
    target.stop_process(process)
    assert process.terminated
    assert remote_calls and "kill -TERM" in remote_calls[0]
    assert ".harness/ssh-owner-" in remote_calls[0]
