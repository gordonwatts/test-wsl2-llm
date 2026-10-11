import json
import os
import posixpath
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_wsl2_llm import runner
from test_wsl2_llm.models import TestConfig as Config
from test_wsl2_llm.storage import (
    MIN_AVAILABLE_BYTES,
    StoragePreflightError,
    check_storage,
    inspect_storage,
    storage_probe_script,
)
from test_wsl2_llm.target import SshTarget


def capacity(directory="/tmp", available_bytes=MIN_AVAILABLE_BYTES, available_inodes=1):
    return {
        "directory": directory,
        "measured_directory": directory,
        "available_bytes": available_bytes,
        "available_inodes": available_inodes,
    }


class Target:
    def __init__(self, entries):
        self.entries = entries
        self.parents = []

    def storage_capacity(self, parent):
        self.parents.append(parent)
        return self.entries


@pytest.mark.parametrize("bytes_free,inodes_free", [(MIN_AVAILABLE_BYTES, 1), (2**40, 5)])
def test_storage_accepts_threshold_and_one_free_inode(bytes_free, inodes_free):
    check_storage(
        Target([capacity(available_bytes=bytes_free, available_inodes=inodes_free)]), "/tmp"
    )


@pytest.mark.parametrize(
    "bytes_free,inodes_free", [(MIN_AVAILABLE_BYTES - 1, 1), (2**40, 0), (0, 0)]
)
def test_storage_rejects_bytes_or_inodes_with_actionable_values(bytes_free, inodes_free):
    with pytest.raises(StoragePreflightError) as caught:
        check_storage(Target([capacity("/runs", bytes_free, inodes_free)]), "/runs")
    message = str(caught.value)
    assert "'/runs'" in message
    assert f"available {bytes_free:,} bytes" in message
    assert "required at least 1,000,000,000 bytes (1 GB)" in message
    assert f"available {inodes_free:,} inodes; required at least 1 inode" in message


def test_storage_reports_both_directories_and_measured_ancestor():
    missing = capacity("/runs/new", 0, 1)
    missing["measured_directory"] = "/runs"
    with pytest.raises(StoragePreflightError) as caught:
        check_storage(Target([capacity(available_inodes=0), missing]), "/runs/new")
    assert "'/tmp'" in str(caught.value)
    assert "'/runs/new' (measured at existing ancestor '/runs')" in str(caught.value)


def mock_posix_probe(monkeypatch, existing=("/tmp", "/runs", "/")):
    # The same source probe runs on remote Linux; simulate its filesystem without
    # relying on Windows statvfs availability or the developer host's /tmp.
    monkeypatch.setitem(
        sys.modules, "pwd", SimpleNamespace(getpwuid=lambda _: SimpleNamespace(pw_dir="/runs"))
    )
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os.path, "realpath", posixpath.normpath)
    monkeypatch.setattr(os.path, "isdir", lambda path: path in existing)
    original_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if str(path) in existing:
            return SimpleNamespace(st_mode=stat.S_IFDIR)
        if str(path).startswith("/runs/"):
            raise FileNotFoundError(str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda _: SimpleNamespace(f_bavail=250_000, f_frsize=4096, f_bsize=4096, f_favail=7),
        raising=False,
    )


def test_probe_deduplicates_canonical_same_directory(monkeypatch):
    with monkeypatch.context() as context:
        mock_posix_probe(context)
        entries = inspect_storage("/tmp/../tmp/")
    assert [entry["directory"] for entry in entries] == ["/tmp"]


def test_probe_uses_existing_ancestor_without_creating_directory(monkeypatch):
    with monkeypatch.context() as context:
        mock_posix_probe(context)
        entries = inspect_storage("/runs/new/nested")
    assert entries[1]["directory"] == "/runs/new/nested"
    assert entries[1]["measured_directory"] == "/runs"
    assert entries[1]["available_bytes"] == 250_000 * 4096


def test_remote_source_matches_local_probe_and_expands_account_home(monkeypatch, capsys):
    with monkeypatch.context() as context:
        mock_posix_probe(context)
        context.setattr(sys, "argv", ["probe", "~/new"])
        exec(storage_probe_script(), {})
    entries = json.loads(capsys.readouterr().out)
    assert entries[1]["directory"] == "/runs/new"


@pytest.mark.parametrize("client_type", [runner.WslClient, SshTarget])
def test_linux_transports_probe_selected_parent_with_quoted_arguments(monkeypatch, client_type):
    calls = []

    def bash(self, script, *arguments, **kwargs):
        calls.append((script, arguments))
        return subprocess.CompletedProcess([], 0, json.dumps([capacity()]).encode(), b"")

    monkeypatch.setattr(client_type, "bash", bash)
    client = client_type("host")
    parent = "/runs with spaces;$literal"
    check_storage(client, parent)
    assert calls[0][0] == 'python3 -c "$1" "$2"'
    assert calls[0][1][1] == parent
    assert "statvfs" in calls[0][1][0]


def test_native_linux_checks_locally_and_windows_macos_skip(monkeypatch):
    parents = []
    monkeypatch.setattr(
        runner, "inspect_storage", lambda parent: parents.append(parent) or [capacity()]
    )
    monkeypatch.setattr(sys, "platform", "linux")
    check_storage(runner.LinuxClient(), "/runs")
    assert parents == ["/runs"]
    monkeypatch.setattr(sys, "platform", "darwin")
    check_storage(runner.LinuxClient(), "/runs")
    monkeypatch.setattr(sys, "platform", "win32")
    check_storage(runner.WindowsClient(), "C:/runs")
    assert parents == ["/runs"]


def test_probe_error_fails_closed_with_context():
    class Broken:
        def storage_capacity(self, parent):
            raise PermissionError("unreadable target")

    with pytest.raises(StoragePreflightError, match="/tmp and workspace parent '/runs'"):
        check_storage(Broken(), "/runs")


def test_unhealthy_storage_aborts_before_agent_or_workspace_and_cancels_admission(monkeypatch):
    from test_wsl2_llm.agents import CodexAgentAdapter

    def unexpected(*args, **kwargs):
        pytest.fail("No agent preflight, workspace creation, or execution may occur")

    monkeypatch.setattr(CodexAgentAdapter, "preflight", unexpected)
    monkeypatch.setattr(runner, "_stream_codex", unexpected)
    monkeypatch.setattr(runner.WslClient, "create_workspace", unexpected)
    target = Target([capacity(available_inodes=0)])
    coordinator = runner.CancellationCoordinator()
    with pytest.raises(StoragePreflightError):
        runner.run_test(
            Config(prompt="hello", model="model", output="out"),
            target=target,
            cancellation=coordinator,
        )
    assert coordinator.cancelled
    assert not coordinator.claim("queued")


def test_ssh_checks_and_creates_under_remote_parent(monkeypatch):
    from test_wsl2_llm.agents import FakeAgentAdapter

    checked = []
    created = []
    monkeypatch.setattr(
        SshTarget, "storage_capacity", lambda self, parent: checked.append(parent) or []
    )
    monkeypatch.setattr(runner, "_resolve_wsl_path", lambda client, path, **kw: path)
    monkeypatch.setattr(FakeAgentAdapter, "preflight", lambda *a: SimpleNamespace(version="fake"))

    def create(self, parent):
        created.append(parent)
        raise RuntimeError("stop after verifying workspace selection")

    monkeypatch.setattr(SshTarget, "create_workspace", create)
    runner.run_test(
        Config(
            prompt="hello",
            model="model",
            output="out",
            agent="fake",
            target="ssh",
            ssh_host="host",
            wsl_parent="/unrelated-wsl",
            remote_workspace_parent="/runs",
        )
    )
    assert checked == created == ["/runs"]


def test_continuation_checks_retained_parent_before_agent(monkeypatch):
    from test_report import sample_result

    from test_wsl2_llm.agents import CodexAgentAdapter

    monkeypatch.setattr(
        CodexAgentAdapter, "preflight", lambda *a: pytest.fail("Agent was launched")
    )
    previous = sample_result()
    monkeypatch.setattr(
        runner, "_collect_evidence", lambda *a, **kw: pytest.fail("Evidence was touched")
    )
    monkeypatch.setattr(runner, "_remove_auth", lambda *a, **kw: pytest.fail("Auth was removed"))
    previous.run.workspace_path = "/runs/retained/workspace"
    target = Target([capacity(available_inodes=0)])
    with pytest.raises(StoragePreflightError):
        runner.continue_test(
            previous, Config(prompt="next", model="model", output="out"), "next", target=target
        )
    assert target.parents == ["/runs"]


@pytest.mark.parametrize("batch", [False, True])
def test_storage_failure_stops_queued_batch_admission(monkeypatch, tmp_path, batch):
    import yaml
    from typer.testing import CliRunner

    from test_wsl2_llm.cli import app

    calls = []

    def abort(config, **kwargs):
        calls.append(config.prompt)
        raise StoragePreflightError("Storage preflight failed: '/tmp' has 0 available inodes")

    monkeypatch.setattr(runner, "run_test", abort)
    monkeypatch.setattr(
        "test_wsl2_llm.report.write_reports",
        lambda *a, **kw: pytest.fail("Failed preflight must not write an agent result"),
    )
    if batch:
        template = tmp_path / "template.yaml"
        template.write_text(
            yaml.safe_dump(
                {
                    "prompt_template": "{{ question }}",
                    "questions": [
                        {"id": "one", "question": "hello"},
                        {"id": "two", "question": "bye"},
                    ],
                    "model": "gpt-test",
                    "output": str(tmp_path / "out"),
                }
            )
        )
        args = ["template", "run", str(template)]
    else:
        args = [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
        ]
    result = CliRunner().invoke(app, [*args, "--repeat", "3", "--threads", "1"])
    assert result.exit_code != 0
    assert len(calls) == 1
    assert "Storage preflight failed" in result.output
    assert "not started: cancellation requested" in result.output


def test_connect_checks_retained_parent_and_aborts_before_launch(monkeypatch, tmp_path):
    import yaml
    from test_report import sample_result
    from typer.testing import CliRunner

    import test_wsl2_llm.cli as cli

    previous = sample_result()
    previous.run.workspace_path = "/runs/retained/workspace"
    report = tmp_path / "result.yaml"
    report.write_text(yaml.safe_dump(previous.model_dump(mode="json")))
    target = Target([capacity(available_inodes=0)])
    monkeypatch.setattr(cli, "create_execution_target", lambda *a, **kw: target)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("Agent was launched"))
    result = CliRunner().invoke(cli.app, ["connect", str(report)])
    assert result.exit_code == 2
    assert "Storage preflight failed" in result.output
    assert target.parents == ["/runs"]
