import subprocess

from test_wsl2_llm.runner import WslClient, _copy_from_target, _copy_to_target


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
