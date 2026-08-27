import re
import subprocess
from io import StringIO
from pathlib import Path

import numpy as np
import pytest
import uproot
from rich.console import Console

from test_wsl2_llm.models import ConversationTurn, CopiedBackFile
from test_wsl2_llm.models import TestConfig as WslTestConfig
from test_wsl2_llm.runner import (
    WslClient,
    _codex_config,
    _console_time,
    _copy_back_files,
    _describe_copied_back,
    _installed_paths_from_json,
    _is_git_marketplace_source,
    _progress_description,
    _root_contents,
    _stream_codex,
    _transfer_files,
    _transfer_marketplaces,
    continuation_prompt,
)


def test_wsl_command_keeps_values_as_separate_arguments() -> None:
    client = WslClient("atlas_al9")
    command = client.command(["bash", "-lc", 'printf "%s" "$1"', "script", "path with spaces;$x"])
    assert command == [
        "wsl.exe",
        "-d",
        "atlas_al9",
        "--",
        "bash",
        "-lc",
        'printf "%s" "$1"',
        "script",
        "path with spaces;$x",
    ]


def test_non_live_codex_progress_prints_to_shared_console(monkeypatch) -> None:
    class FakeStdin:
        def write(self, _value: str) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeProcess:
        stdin = FakeStdin()
        stdout = StringIO('{"type":"item.started"}\n')
        stderr = StringIO("plain stderr\n")

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        "test_wsl2_llm.runner.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess()
    )
    rendered = StringIO()
    logs: list[str] = []
    exit_code, stdout, stderr, _traces, _events = _stream_codex(
        ["codex"],
        "hello",
        progress_lines=5,
        verbosity=0,
        console=Console(file=rendered),
        live_progress=False,
        log_callback=logs.append,
    )

    assert exit_code == 0
    assert stdout == '{"type":"item.started"}\n'
    assert stderr == "plain stderr\n"
    assert any("item.started" in line for line in logs)
    assert any("plain stderr" in line for line in logs)
    assert rendered.getvalue() == ""


def test_progress_description_is_human_readable_and_bounded() -> None:
    description = _progress_description(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "echo " + "x" * 500,
                "exit_code": 0,
            },
        },
        "ignored",
    )
    assert description.startswith("Completed command (exit 0): echo")
    assert len(description) <= 120
    assert "item.completed" not in description


def test_codex_config_enables_auto_review_network_and_workspace_write(tmp_path) -> None:
    config = WslTestConfig(
        prompt="hello",
        model="gpt-test",
        output=str(tmp_path / "out"),
    )
    text = _codex_config(config)
    assert 'approval_policy = "on-request"' in text
    assert 'approvals_reviewer = "auto_review"' in text
    assert 'sandbox_mode = "workspace-write"' in text
    assert 'model_reasoning_effort = "medium"' in text
    assert "network_access = true" in text


def test_shell_command_escapes_wsl_dollar_expansion_and_encodes_values() -> None:
    command = WslClient("atlas_al9").shell_command('printf "%s" "$1"', "space ; $value")
    assert command[:2] == ["env", "TEST_WSL2_LLM_ARG_0=c3BhY2UgOyAkdmFsdWU="]
    assert "\\$TEST_WSL2_LLM_ARG_0" in command[-1]
    assert "\\$1" in command[-1]


def test_installed_plugin_paths_are_extracted_from_nested_json() -> None:
    output = '{"plugin": {"installedPath": "/tmp/codex/plugins/demo"}}'
    assert _installed_paths_from_json(output) == ["/tmp/codex/plugins/demo"]


def test_progress_display_is_limited_to_five_lines(tmp_path) -> None:
    with pytest.raises(ValueError, match="between 1 and 5"):
        WslTestConfig(
            prompt="hello",
            model="gpt-test",
            output=str(tmp_path / "out"),
            progress_lines=6,
        )


def test_progress_receipt_time_is_formatted_without_a_date() -> None:
    displayed = _console_time("2026-08-08T20:15:09.123456+00:00")
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", displayed)
    assert "2026" not in displayed


@pytest.mark.parametrize(
    "source",
    [
        "https://github.com/example/marketplace.git",
        "ssh://git@github.com/example/marketplace.git",
        "git@github.com:example/marketplace.git",
    ],
)
def test_git_marketplace_source_recognition(source: str) -> None:
    assert _is_git_marketplace_source(source)


def test_git_marketplace_is_cloned_into_wsl_harness() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, tuple[str, ...]]] = []

        def bash(self, script: str, *arguments: str, **_kwargs):
            self.calls.append(("bash", script, arguments))
            return subprocess.CompletedProcess([], 0, b"", b"")

        def login_bash(self, script: str, *arguments: str, **_kwargs):
            self.calls.append(("login_bash", script, arguments))
            return subprocess.CompletedProcess([], 0, b"", b"")

    client = RecordingClient()
    source = "https://github.com/example/marketplace.git"
    runtime = _transfer_marketplaces(client, [source], "/tmp/test-wsl2-llm-run")  # type: ignore[arg-type]

    destination = "/tmp/test-wsl2-llm-run/.harness/inputs/marketplaces/marketplace-001"
    assert runtime == [destination]
    assert ("login_bash", 'git clone --depth 1 -- "$1" "$2"', (source, destination)) in client.calls


def test_copy_files_are_transferred_to_workspace_root(tmp_path) -> None:
    source = tmp_path / "servicex.yaml"
    source.write_text("token: secret\n", encoding="utf-8")

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, tuple[str, ...]]] = []

        def bash(self, script: str, *arguments: str, **_kwargs):
            self.calls.append(("bash", script, arguments))
            if script == 'wslpath -a "$1"':
                return subprocess.CompletedProcess([], 0, b"/mnt/c/servicex.yaml\n", b"")
            return subprocess.CompletedProcess([], 0, b"", b"")

        def text(self, completed) -> str:
            return completed.stdout.decode()

    client = RecordingClient()
    _transfer_files(client, [str(source)], "/tmp/run/workspace")  # type: ignore[arg-type]

    assert ("bash", 'wslpath -a "$1"', (str(source.resolve()),)) in client.calls
    assert (
        "bash",
        'cp -- "$2" "$1/"',
        ("/tmp/run/workspace", "/mnt/c/servicex.yaml"),
    ) in client.calls


def test_copy_back_expands_wildcards_and_uses_output_stub(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "run.plot.txt"
    destination.write_text("one\ntwo\n", encoding="utf-8")
    described = _describe_copied_back("plot.txt", destination)
    assert described.type == "text"
    assert described.text_preview == "one\ntwo"

    png = tmp_path / "plot.png"
    png.write_bytes(b"png")
    png_description = _describe_copied_back("plot.png", png)
    assert png_description.type == "image"

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, tuple[str, ...]]] = []

        def bash(self, script: str, *arguments: str, **_kwargs):
            self.calls.append(("bash", script, arguments))
            if script == 'wslpath -a "$1"':
                destination = arguments[0].replace("\\", "/").rsplit("/", 1)[-1]
                return subprocess.CompletedProcess(
                    [], 0, f"/mnt/c/results/{destination}\n".encode(), b""
                )
            if "compgen -G" in script:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    b"/tmp/run/workspace/plot_1.png\0/tmp/run/workspace/plot_2.png\0",
                    b"",
                )
            return subprocess.CompletedProcess([], 0, b"", b"")

        def text(self, completed) -> str:
            return completed.stdout.decode()

    monkeypatch.setattr(
        "test_wsl2_llm.runner._describe_copied_back",
        lambda source, path: CopiedBackFile(
            source=source, destination=str(path), type="file", size=1
        ),
    )
    client = RecordingClient()
    copied = _copy_back_files(client, ["plot_*.png"], "/tmp/run/workspace", str(tmp_path / "run"))
    assert [item.source for item in copied] == ["plot_1.png", "plot_2.png"]
    assert [Path(item.destination).name for item in copied] == [
        "run.plot_1.png",
        "run.plot_2.png",
    ]
    cp_calls = [
        arguments
        for _, script, arguments in client.calls
        if script == 'cp -- "$1" "$2"'
    ]
    assert cp_calls == [
        ("/tmp/run/workspace/plot_1.png", "/mnt/c/results/run.plot_1.png"),
        ("/tmp/run/workspace/plot_2.png", "/mnt/c/results/run.plot_2.png"),
    ]


def test_copy_back_pattern_without_matches_is_recorded_and_skipped() -> None:
    class EmptyClient:
        def bash(self, script: str, *arguments: str, **_kwargs):
            return subprocess.CompletedProcess([], 0, b"", b"")

    missing: list[str] = []
    copied = _copy_back_files(
        EmptyClient(),
        ["missing_*.png", "also-missing.txt"],
        "/tmp/run/workspace",
        "C:/results/run",
        missing=missing,
    )
    assert copied == []
    assert missing == ["missing_*.png", "also-missing.txt"]


def test_copy_back_limits_matches(tmp_path, monkeypatch) -> None:
    class RecordingClient:
        def bash(self, script: str, *arguments: str, **_kwargs):
            if "compgen -G" in script:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    b"/tmp/run/workspace/plot_1.png\0/tmp/run/workspace/plot_2.png\0",
                    b"",
                )
            if script == 'wslpath -a "$1"':
                return subprocess.CompletedProcess([], 0, b"/mnt/c/out.png\n", b"")
            return subprocess.CompletedProcess([], 0, b"", b"")

        def text(self, completed) -> str:
            return completed.stdout.decode()

    monkeypatch.setattr(
        "test_wsl2_llm.runner._describe_copied_back",
        lambda source, path: CopiedBackFile(
            source=source, destination=str(path), type="file", size=1
        ),
    )
    copied = _copy_back_files(
        RecordingClient(), ["plot_*.png"], "/tmp/run/workspace", str(tmp_path / "run"), max_files=1
    )
    assert [item.source for item in copied] == ["plot_1.png"]


def test_copy_back_pattern_without_matches_can_be_collected_without_error(tmp_path) -> None:
    class EmptyClient:
        def bash(self, script: str, *arguments: str, **_kwargs):
            return subprocess.CompletedProcess([], 0, b"", b"")

    missing: list[str] = []
    assert _copy_back_files(
        EmptyClient(),
        ["missing_*.png"],
        "/tmp/run/workspace",
        str(tmp_path / "run"),
        missing=missing,
    ) == []
    assert missing == ["missing_*.png"]


def test_root_contents_lists_ttree_branches_and_events(tmp_path) -> None:
    path = tmp_path / "events.root"
    with uproot.recreate(path) as root_file:
        root_file.mktree("events", {"pt": "float64", "run": "int32"})
        root_file["events"].extend({"pt": np.array([1.0, 2.0]), "run": np.array([10, 11])})

    contents = _root_contents(path)
    tree = next(item for item in contents if item["path"].startswith("events"))
    assert tree["type"] == "TTree"
    assert tree["events"] == 2
    assert tree["branches"] == ["pt", "run"]


def test_continuation_prompt_contains_prior_chain_and_new_prompt() -> None:
    prompt = continuation_prompt(
        [ConversationTurn(prompt="Create a file", final_response="Created it.")],
        "Now inspect the file.",
    )
    assert (
        "This working directory was created with the following list of prompts and responses."
        in prompt
    )
    assert "Prompt 1:\nCreate a file" in prompt
    assert "Final Response:\nCreated it." in prompt
    assert prompt.endswith("New prompt:\nNow inspect the file.")
