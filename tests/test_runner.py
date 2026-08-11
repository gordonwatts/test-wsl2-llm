import re
import subprocess

import pytest

from test_wsl2_llm.models import ConversationTurn
from test_wsl2_llm.models import TestConfig as WslTestConfig
from test_wsl2_llm.runner import (
    WslClient,
    _codex_config,
    _console_time,
    _installed_paths_from_json,
    _is_git_marketplace_source,
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
