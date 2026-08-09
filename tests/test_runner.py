import re

import pytest

from test_wsl2_llm.models import TestConfig as WslTestConfig
from test_wsl2_llm.runner import (
    WslClient,
    _codex_config,
    _console_time,
    _installed_paths_from_json,
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
