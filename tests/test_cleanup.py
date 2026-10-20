import json
import subprocess
from pathlib import Path

import pytest
import yaml
from test_report import sample_result
from typer.testing import CliRunner

from test_wsl2_llm import runner
from test_wsl2_llm.cli import app
from test_wsl2_llm.config import load_config_file
from test_wsl2_llm.models import CopiedBackFile, SessionTrace
from test_wsl2_llm.models import TestConfig as Config
from test_wsl2_llm.report import render_markdown


@pytest.fixture(autouse=True)
def healthy_mock_storage(monkeypatch):
    """Lifecycle tests replace the transport; storage has its own probe tests."""
    monkeypatch.setattr(runner.WslClient, "storage_capacity", lambda self, parent: [])


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


@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("child_model", ["gpt-6-luna", "gpt-6.1-sol"])
def test_runner_prices_subagent_trace_without_recounting_history(
    monkeypatch, tmp_path, continuation, child_model
):
    def bash(self, script, *args, **kwargs):
        return subprocess.CompletedProcess([], 0, b"/tmp/test-wsl2-llm-abcdefgh", b"")

    parent_events = [
        {"type": "thread.started", "thread_id": "parent"},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
        },
    ]
    child_rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
            },
        },
        {"type": "turn_context", "payload": {"model": child_model}},
    ]

    def token_event(input_tokens, output_tokens):
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": 0,
                        "output_tokens": output_tokens,
                    }
                },
            },
        }

    old_content = "\n".join(json.dumps(row) for row in [*child_rows, token_event(30, 2)]) + "\n"
    content = old_content + json.dumps(token_event(50, 5)) + "\n" if continuation else old_content
    trace = SessionTrace(path="sessions/child.jsonl", content=content)
    monkeypatch.setattr(runner.WslClient, "bash", bash)
    monkeypatch.setattr(runner.WslClient, "login_bash", bash)
    monkeypatch.setattr(
        runner,
        "_stream_codex",
        lambda *a, **k: (
            0, "\n".join(json.dumps(event) for event in parent_events), "", [], parent_events
        ),
    )
    monkeypatch.setattr(runner, "_inventory", lambda *a: [])
    monkeypatch.setattr(runner, "_copy_back_files", lambda *a, **k: [])
    monkeypatch.setattr(runner, "_session_traces", lambda *a: ([trace], []))
    config = Config(prompt="hello", model="gpt-6.1-sol", output=str(tmp_path / "out"))
    previous = sample_result()
    previous.logs.session_traces = [SessionTrace(path=trace.path, content=old_content)]
    result = (
        runner.continue_test(previous, config, "follow up")
        if continuation
        else runner.run_test(config)
    )
    assert [(item.model, item.input_tokens, item.output_tokens) for item in result.usage] == [
        ("gpt-6.1-sol", 100, 10),
        (child_model, 20 if continuation else 30, 3 if continuation else 2),
    ]
    assert len(result.model_information.models) == 2
    assert result.model_information.total_cost == sum(
        item.total_cost for item in result.model_information.models
    )


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


@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("failure_stage", ["setup", "execution"])
def test_shared_finalization_keeps_evidence_after_interruptions(
    monkeypatch, tmp_path, continuation, failure_stage
):
    from unittest.mock import MagicMock

    from test_wsl2_llm.models import CopiedBackFile
    from test_wsl2_llm.models import TestConfig as RunConfig

    client = MagicMock()
    client.text.return_value = "/tmp/test-wsl2-llm-abcdefgh"
    client.command.return_value = ["mock-codex"]

    def bash(script, *args, **kwargs):
        if "rm -f" in script:
            raise KeyboardInterrupt()
        return subprocess.CompletedProcess([], 0, b"/tmp/test-wsl2-llm-abcdefgh", b"")

    monkeypatch.setattr(runner, "WslClient", lambda *args: client)
    client.bash.side_effect = bash
    client.login_bash.side_effect = bash
    monkeypatch.setattr(runner, "_resolve_wsl_path", lambda *args, **kwargs: "/tmp/auth.json")
    monkeypatch.setattr(runner, "_transfer_marketplaces", lambda *args, **kwargs: [])
    monkeypatch.setattr(runner, "_transfer_files", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_skill_directories", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_inventory",
        lambda *args: (_ for _ in ()).throw(RuntimeError("inventory broken")),
    )
    artifact = CopiedBackFile(source="proof.txt", destination="proof.txt", type="text", size=2)
    monkeypatch.setattr(runner, "_copy_back_files", lambda *args, **kwargs: [artifact])
    monkeypatch.setattr(
        runner,
        "_session_traces",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    if failure_stage == "setup":
        monkeypatch.setattr(
            runner,
            "_write_wsl_file",
            lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    else:
        monkeypatch.setattr(
            runner,
            "_write_wsl_file",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            runner,
            "_stream_codex",
            lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    config = RunConfig(
        prompt="hello", model="gpt-test", output=str(tmp_path / "out"), cleanup=not continuation
    )
    previous = sample_result()
    result = (
        runner.continue_test(previous, config, "follow up")
        if continuation
        else runner.run_test(config, live_progress=False)
    )

    assert result.copied_back == [artifact]
    assert result.run.status == "failed"
    assert "inventory broken" in result.run.error
    assert "KeyboardInterrupt" in result.run.error
    assert "auth cleanup failed" in result.run.error
    assert [phase.name for phase in result.timing.phases if phase.name == "copy_back"]
    if continuation:
        assert result.run.workspace_retained
        assert result.continued_from == previous.run.workspace_path
        assert result.conversation[0].prompt == previous.prompt
        assert result.conversation[-1].prompt == "follow up"
        assert result.skills.plugins == previous.skills.plugins
