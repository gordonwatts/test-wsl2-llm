import base64
import subprocess

import pytest

from test_wsl2_llm.models import TestConfig as Config
from test_wsl2_llm.runner import WindowsClient, _check_codex_home
from test_wsl2_llm.target import SshTarget


def test_default_parents_keep_isolated_homes_outside_system_temp() -> None:
    config = Config(prompt="hello", model="test", output="result")
    assert config.wsl_parent == "~/"
    assert config.remote_workspace_parent == "~/"
    assert SshTarget("example").remote_workspace_parent == "~/"


@pytest.mark.parametrize("refused", [False, True])
def test_isolated_home_check_catches_warning_despite_successful_exit(refused: bool) -> None:
    calls = []
    warning = (
        b"WARNING: proceeding, even though we could not create PATH aliases: "
        b'Refusing to create helper binaries under temporary dir "/tmp"'
        if refused else b""
    )

    class Target:
        def login_bash(self, script, *args):
            calls.append((script, args))
            return subprocess.CompletedProcess([], 0, b"codex-cli 0.163.0", warning)

    def gate():
        calls.append("gate")

    if refused:
        with pytest.raises(RuntimeError, match="Choose wsl_parent.*outside"):
            _check_codex_home(Target(), "/tmp/run/.harness/codex-home", before_launch=gate)
    else:
        _check_codex_home(Target(), "/home/user/run/.harness/codex-home", before_launch=gate)

    assert calls[0] == "gate"
    assert calls[1][0] == 'env CODEX_HOME="$1" codex --version'
    assert calls[1][1][0].endswith("/.harness/codex-home")


def test_windows_home_check_sets_isolated_home() -> None:
    command = WindowsClient().shell_command(
        'env CODEX_HOME="$1" codex --version', r"C:\Users\example\run\.harness\codex-home"
    )
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    assert "$env:CODEX_HOME = $testWsl2Arg0" in script
    assert "& codex --version" in script
