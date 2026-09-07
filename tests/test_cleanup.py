import subprocess
from pathlib import Path

import pytest
import yaml
from test_report import sample_result
from typer.testing import CliRunner

from test_wsl2_llm import runner
from test_wsl2_llm.cli import app
from test_wsl2_llm.config import load_config_file
from test_wsl2_llm.models import CopiedBackFile
from test_wsl2_llm.models import TestConfig as Config
from test_wsl2_llm.report import render_markdown


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("exit_code", [0, 1, 124, 130])
@pytest.mark.parametrize("keep", [False, True])
def test_cleanup_lifecycle(monkeypatch, tmp_path, exit_code, keep, cleanup_fails):
    calls = []

    def bash(self, script, *args, **kwargs):
        calls.append(script)
        if "rm -rf" in script and cleanup_fails:
            raise RuntimeError("cannot remove workspace")
        output = b"/tmp/test-wsl2-llm-abcdefgh" if "mktemp" in script else b""
        return subprocess.CompletedProcess([], 0, output, b"")

    monkeypatch.setattr(runner.WslClient, "bash", bash)
    monkeypatch.setattr(runner.WslClient, "login_bash", bash)
    monkeypatch.setattr(runner, "_stream_codex", lambda *a, **k: (exit_code, "", "", [], []))
    monkeypatch.setattr(runner, "_inventory", lambda *a: [])
    monkeypatch.setattr(runner, "_copy_back_files", lambda *a, **k: [])
    monkeypatch.setattr(runner, "_session_traces", lambda *a: ([], []))
    config = Config(
        prompt="hello", model="gpt-test", output=str(tmp_path / "out"), cleanup=not keep
    )

    def report_callback(result):
        assert not any("rm -rf" in call for call in calls)
        assert result.run.workspace_retained

    result = runner.run_test(config, report_callback=report_callback)
    assert result.run.workspace_retained is (keep or cleanup_fails)
    assert bool(result.run.workspace_path) is (keep or cleanup_fails)
    assert any("rm -rf" in call for call in calls) is not keep
    assert result.run.exit_code == (exit_code or (1 if cleanup_fails and not keep else 0))
    if cleanup_fails and not keep:
        assert "workspace cleanup failed" in result.run.error
    if not keep and not cleanup_fails:
        assert "(removed)" in render_markdown(result)


@pytest.mark.parametrize("failure", [RuntimeError("inventory broken"), KeyboardInterrupt()])
def test_collection_failure_still_copies_artifacts_and_cleans(monkeypatch, tmp_path, failure):
    calls = []

    def bash(self, script, *args, **kwargs):
        calls.append(script)
        return subprocess.CompletedProcess([], 0, b"/tmp/test-wsl2-llm-abcdefgh", b"")

    def inventory(*args):
        raise failure

    monkeypatch.setattr(runner.WslClient, "bash", bash)
    monkeypatch.setattr(runner.WslClient, "login_bash", bash)
    monkeypatch.setattr(runner, "_stream_codex", lambda *a, **k: (0, "", "", [], []))
    monkeypatch.setattr(runner, "_inventory", inventory)
    artifacts = [CopiedBackFile(source="proof.txt", destination="proof.txt", type="text", size=2)]
    monkeypatch.setattr(runner, "_copy_back_files", lambda *a, **k: artifacts)
    monkeypatch.setattr(runner, "_session_traces", lambda *a: ([], []))
    result = runner.run_test(Config(prompt="hi", model="gpt-test", output=str(tmp_path / "out")))
    assert not result.run.workspace_retained
    assert result.run.status == "failed"
    assert result.copied_back == artifacts
    assert any("rm -rf" in call for call in calls)


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_cli_cleanup_for_every_repetition(monkeypatch, tmp_path: Path, keep, batch):
    values = []

    def fake_run(config, **kwargs):
        values.append(config.cleanup)
        return sample_result()

    monkeypatch.setattr(runner, "run_test", fake_run)
    monkeypatch.setattr(
        "test_wsl2_llm.report.write_reports", lambda *a: (tmp_path / "x.md", tmp_path / "x.yaml")
    )
    if batch:
        path = tmp_path / "batch.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "prompt_template": "{{ question }}",
                    "questions": [
                        {"id": "one", "question": "hi"},
                        {"id": "two", "question": "bye"},
                    ],
                    "model": "gpt-test",
                    "output": str(tmp_path / "out"),
                }
            )
        )
        args = ["template", "run", str(path)]
    else:
        args = ["run", "--prompt", "hi", "--model", "gpt-test", "--output", str(tmp_path / "out")]
    result = CliRunner().invoke(
        app, [*args, "--repeat", "2", *(["--keep-workspace"] if keep else [])]
    )
    assert result.exit_code == 0, result.output
    assert values == [not keep] * (4 if batch else 2)


@pytest.mark.parametrize("keep", [False, True])
def test_saved_cleanup_roundtrips(tmp_path, keep):
    saved = tmp_path / "config.yaml"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--prompt",
            "hi",
            "--model",
            "gpt-test",
            "--save-config",
            str(saved),
            "--config-only",
            *(["--keep-workspace"] if keep else []),
        ],
    )
    assert result.exit_code == 0, result.output
    assert Config.model_validate(load_config_file(saved)).cleanup is not keep


@pytest.mark.parametrize("command", ["connect", "continue"])
def test_cleaned_result_explains_keep_option(tmp_path, command):
    result = sample_result()
    result.run.workspace_retained = False
    result.run.workspace_path = None
    path = tmp_path / "result.yaml"
    path.write_text(yaml.safe_dump(result.model_dump(mode="json")))
    extra = ["--prompt", "more"] if command == "continue" else []
    invoked = CliRunner().invoke(app, [command, str(path), *extra])
    assert invoked.exit_code == 2
    assert "--keep-workspace" in invoked.output
