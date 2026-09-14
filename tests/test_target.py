import subprocess
from pathlib import Path

from test_wsl2_llm.runner import (
    LinuxClient,
    WslClient,
    _copy_from_target,
    _copy_to_target,
    create_execution_target,
)


def test_wsl_target_workspace_lifecycle_uses_target_native_paths(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_bash(self, script: str, *arguments: str, **_kwargs):
        calls.append((script, arguments))
        output = b"/tmp/run with spaces\n" if script.startswith("mktemp") else b""
        return subprocess.CompletedProcess([], 0, output, b"")

    monkeypatch.setattr(WslClient, "bash", fake_bash)
    client = WslClient("atlas_al9")

    run_root = client.create_workspace("/tmp/parent with spaces")
    client.cleanup_workspace(run_root)

    assert run_root == "/tmp/run with spaces"
    assert calls == [
        ('mktemp -d -p "$1" test-wsl2-llm-XXXXXXXX', ("/tmp/parent with spaces",)),
        (
            'mkdir -p "$1" "$2"',
            ("/tmp/run with spaces/workspace", "/tmp/run with spaces/.harness/inputs"),
        ),
        ('rm -rf -- "$1"', ("/tmp/run with spaces",)),
    ]


def test_wsl_target_file_transfers_keep_host_values_as_arguments(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_bash(self, script: str, *arguments: str, **_kwargs):
        calls.append((script, arguments))
        if script == 'wslpath -a "$1"':
            return subprocess.CompletedProcess([], 0, b"/mnt/c/path with spaces/$x\n", b"")
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(WslClient, "bash", fake_bash)
    client = WslClient("atlas_al9")
    host = r"C:\path with spaces\$x\café.txt"
    _copy_to_target(client, host, "/tmp/work space")
    _copy_from_target(client, "/tmp/work space/result $x.txt", host)

    assert calls == [
        ('wslpath -a "$1"', (host,)),
        ('cp -- "$2" "$1/"', ("/tmp/work space", "/mnt/c/path with spaces/$x")),
        ('wslpath -a "$1"', (host,)),
        ('cp -- "$1" "$2"', ("/tmp/work space/result $x.txt", "/mnt/c/path with spaces/$x")),
    ]


def test_stream_process_uses_target_environment(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("test_wsl2_llm.runner.subprocess.Popen", fake_popen)
    client = WslClient(source_environment={"Path": "C:/tools"})
    client.start_process(["wsl.exe", "--", "true"])

    assert captured["command"] == ["wsl.exe", "--", "true"]
    assert captured["env"] == {"Path": "C:/tools"}
    assert captured["text"] is True


def test_linux_target_uses_native_commands_and_preserves_arguments() -> None:
    client = LinuxClient(source_environment={"PATH": "/bin"})

    assert client.command(["codex", "--model", "model;$(touch nope)"]) == [
        "codex",
        "--model",
        "model;$(touch nope)",
    ]
    assert client.shell_command("printf '%s' \"$1\"", "space $x; café") == [
        "bash",
        "-lc",
        "printf '%s' \"$1\"",
        "test-wsl2-llm",
        "space $x; café",
    ]
    assert isinstance(create_execution_target(execution_target="linux"), LinuxClient)


def test_linux_target_workspace_and_transfers_preserve_unicode_and_symlinks(tmp_path) -> None:
    source = tmp_path / "input $x café.txt"
    source.write_text("hello", encoding="utf-8")
    parent = tmp_path / "parent"
    client = LinuxClient()

    run_root = client.create_workspace(str(parent))
    workspace = Path(run_root) / "workspace"
    client.copy_to_target(str(source), str(workspace))
    copied = workspace / source.name
    destination = tmp_path / "output $x café.txt"
    client.copy_from_target(str(copied), str(destination))

    assert destination.read_text(encoding="utf-8") == "hello"
    client.cleanup_workspace(run_root)
    assert not Path(run_root).exists()


def test_ssh_target_can_be_selected_with_public_connection_settings() -> None:
    from test_wsl2_llm.target import SshTarget

    client = create_execution_target(
        execution_target="ssh",
        ssh_host="build-alias",
        ssh_user="runner",
        ssh_port=2222,
        remote_workspace_parent="/srv/runs",
    )
    assert isinstance(client, SshTarget)
    assert client.destination == "runner@build-alias"
    assert client.remote_workspace_parent == "/srv/runs"
