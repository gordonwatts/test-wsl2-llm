import subprocess
from pathlib import Path
from threading import Barrier, Lock

import yaml
from test_report import sample_result
from typer.testing import CliRunner

import test_wsl2_llm.cli as cli_module
from test_wsl2_llm.cli import _connect_command, app
from test_wsl2_llm.config import DEFAULT_CONFIG_ENV, output_paths

runner = CliRunner()


def test_help_documents_run_and_core_options() -> None:
    top = runner.invoke(app, ["--help"])
    run = runner.invoke(app, ["run", "--help"])
    assert top.exit_code == 0
    assert "run" in top.stdout
    assert run.exit_code == 0
    for option in (
        "--prompt",
        "--config",
        "--force",
        "--marketplace",
        "--output",
        "--pricing-file",
        "--save-config",
        "--title",
        "--copy-file",
        "--copy-back",
        "--unset-env",
        "--path-remove",
        "--repeat",
        "--threads",
        "--timeout",
        "--max-copy-back",
    ):
        assert option in run.stdout
    assert "--overwrite" not in run.stdout
    assert "connect" in top.stdout
    assert "continue" in top.stdout


def test_repeat_indexes_each_run_output(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    written: list[str] = []

    def fake_run(config, **_kwargs):
        calls.append(config.output)
        return sample_result()

    def fake_write(result, output, overwrite=False):
        del result, overwrite
        written.append(output)
        return output_paths(output)

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr("test_wsl2_llm.report.write_reports", fake_write)

    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out.md"),
            "--repeat",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    expected = [str(tmp_path / f"out-{index:03d}") for index in range(1, 4)]
    assert calls == expected
    assert written == expected
    assert "Repeat 1/3" in result.output
    assert "Repeat 3/3" in result.output


def test_threads_run_repetitions_concurrently(monkeypatch, tmp_path: Path) -> None:
    barrier = Barrier(4)
    lock = Lock()
    active = 0
    maximum_active = 0
    calls: list[str] = []

    def fake_run(config, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            calls.append(config.output)
        try:
            barrier.wait(timeout=2)
            return sample_result()
        finally:
            with lock:
                active -= 1

    def fake_write(result, output, overwrite=False):
        del result, overwrite
        return output_paths(output)

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr("test_wsl2_llm.report.write_reports", fake_write)

    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--repeat",
            "4",
            "--threads",
            "4",
        ],
    )

    assert result.exit_code == 0, result.output
    assert maximum_active == 4
    assert sorted(calls) == sorted(str(tmp_path / f"out-{index:03d}") for index in range(1, 5))


def test_progress_is_transient_and_only_used_for_repeats(monkeypatch, tmp_path: Path) -> None:
    progress_instances: list[dict[str, object]] = []
    live_progress_values: list[bool] = []

    class RecordingRepeatDisplay:
        def __init__(self, _console, total):
            progress_instances.append({"total": total, "advances": 0})
            self.state = progress_instances[-1]

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def log(self, _line):
            pass

        def advance(self):
            self.state["advances"] = int(self.state["advances"]) + 1

    def fake_run(_config, **kwargs):
        live_progress_values.append(kwargs["live_progress"])
        return sample_result()

    def fake_write(result, output, overwrite=False):
        del result, overwrite
        return output_paths(output)

    monkeypatch.setattr(cli_module, "_RepeatDisplay", RecordingRepeatDisplay)
    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr("test_wsl2_llm.report.write_reports", fake_write)

    repeated = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "repeated"),
            "--repeat",
            "2",
        ],
    )
    single = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "single"),
        ],
    )

    assert repeated.exit_code == 0, repeated.output
    assert single.exit_code == 0, single.output
    assert len(progress_instances) == 1
    assert progress_instances[0]["total"] == 2
    assert progress_instances[0]["advances"] == 2
    assert live_progress_values == [False, False, True]


def test_repeat_rejects_non_positive_count(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--repeat",
            "0",
        ],
    )
    assert result.exit_code == 2
    assert "Invalid value for '--repeat'" in result.output


def test_threads_rejects_non_positive_count(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--threads",
            "0",
        ],
    )
    assert result.exit_code == 2
    assert "Invalid value for '--threads'" in result.output


def test_continue_config_only_uses_new_prompt_and_saved_output(tmp_path: Path) -> None:
    source = tmp_path / "previous.yaml"
    destination = tmp_path / "continued.yaml"
    result = sample_result()
    result.configuration = {
        "model": "gpt-previous",
        "reasoning_effort": "high",
        "sandbox": "read-only",
        "network": False,
        "approval_policy": "never",
        "approvals_reviewer": "user",
        "auth_source": "~/.codex/previous-auth.json",
        "progress_lines": 3,
        "copy_files": ["C:/secrets/servicex.yaml"],
        "marketplaces": ["https://example.com/previous-marketplace.git"],
        "plugins": ["previous-plugin@previous-marketplace"],
    }
    source.write_text(yaml.safe_dump(result.model_dump(mode="json")), encoding="utf-8")
    invoked = runner.invoke(
        app,
        [
            "continue",
            str(source),
            "--prompt",
            "Inspect the existing file.",
            "--output",
            str(tmp_path / "next"),
            "--save-config",
            str(destination),
            "--config-only",
        ],
    )
    assert invoked.exit_code == 0, invoked.output
    saved = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert saved["prompt"] == "Inspect the existing file."
    assert saved["output"] == str((tmp_path / "next").resolve())
    assert saved["cleanup"] is False
    assert saved["model"] == "gpt-previous"
    assert saved["reasoning_effort"] == "high"
    assert saved["sandbox"] == "read-only"
    assert saved["network"] is False
    assert saved["approval_policy"] == "never"
    assert saved["approvals_reviewer"] == "user"
    assert saved["auth_source"] == "~/.codex/previous-auth.json"
    assert saved["progress_lines"] == 3
    assert saved["copy_files"] == [str(Path("C:/secrets/servicex.yaml"))]
    assert saved["marketplaces"] == ["https://example.com/previous-marketplace.git"]
    assert saved["plugins"] == ["previous-plugin@previous-marketplace"]


def test_continue_can_inherit_a_previous_continuation(tmp_path: Path) -> None:
    source = tmp_path / "previous.yaml"
    destination = tmp_path / "continued.yaml"
    result = sample_result()
    result.configuration = {
        "prompt": result.prompt,
        "title": result.title,
        "model": "gpt-previous",
        "output": str(tmp_path / "previous"),
        "continuation_of": "/tmp/retained-workspace",
    }
    source.write_text(yaml.safe_dump(result.model_dump(mode="json")), encoding="utf-8")
    invoked = runner.invoke(
        app,
        [
            "continue",
            str(source),
            "--prompt",
            "Continue the continued run.",
            "--output",
            str(tmp_path / "next"),
            "--save-config",
            str(destination),
            "--config-only",
        ],
    )
    assert invoked.exit_code == 0, invoked.output
    saved = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert saved["prompt"] == "Continue the continued run."
    assert saved["model"] == "gpt-previous"
    assert "continuation_of" not in saved


def test_connect_command_targets_retained_workspace_and_resume() -> None:
    result = sample_result()
    command = _connect_command(result, resume=True)
    assert "wsl.exe" in command[0]
    script = command[-1]
    assert "codex resume --last --cd" in script
    assert "workspace" in script and "\\$workspace" in script


def test_connect_applies_saved_environment_policy(monkeypatch, tmp_path: Path) -> None:
    result = sample_result()
    result.configuration["environment"] = {
        "unset": ["TEST_WSL2_LLM_REMOVE"],
        "path_remove": [],
    }
    source = tmp_path / "result.yaml"
    source.write_text(yaml.safe_dump(result.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setenv("TEST_WSL2_LLM_REMOVE", "secret value")
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    invoked = runner.invoke(app, ["connect", str(source)])

    assert invoked.exit_code == 0, invoked.output
    assert "TEST_WSL2_LLM_REMOVE" not in captured["environment"]


def test_connect_rejects_cleaned_up_result(tmp_path: Path) -> None:
    source = tmp_path / "result.yaml"
    result = sample_result()
    result.run.workspace_retained = False
    source.write_text(yaml.safe_dump(result.model_dump(mode="json")), encoding="utf-8")
    invoked = runner.invoke(app, ["connect", str(source)])
    assert invoked.exit_code == 2
    assert "not retained" in invoked.output


def test_result_commands_explain_yaml_parse_failures(tmp_path: Path) -> None:
    source = tmp_path / "result.md"
    source.write_text("## Final response\n\n```text\nnot YAML\n```\n", encoding="utf-8")
    invoked = runner.invoke(
        app,
        [
            "continue",
            str(source),
            "--prompt",
            "Inspect the existing file.",
            "--config-only",
            "--save-config",
            str(tmp_path / "saved.yaml"),
        ],
    )
    assert invoked.exit_code == 2
    assert "while trying to parse file" in invoked.output
    assert "result.md" in invoked.output
    assert "as YAML" in invoked.output
    assert "found character '`'" in invoked.output


def test_config_only_writes_resolved_yaml_without_wsl(tmp_path: Path) -> None:
    destination = tmp_path / "saved.yaml"
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--unset-env",
            "INCLUDE",
            "--path-remove",
            r"C:\Program Files\Microsoft Visual Studio",
            "--save-config",
            str(destination),
            "--config-only",
        ],
    )
    assert result.exit_code == 0, result.stdout
    saved = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert saved["prompt"] == "hello"
    assert saved["title"] == "# WSL2 Codex test result"
    assert saved["environment"] == {
        "unset": ["INCLUDE"],
        "path_remove": [r"C:\Program Files\Microsoft Visual Studio"],
    }
    assert not (tmp_path / "out.yaml").exists()


def test_explicit_config_composes_with_user_defaults(
    monkeypatch, tmp_path: Path
) -> None:
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text(
        "environment:\n"
        "  unset: [INCLUDE]\n"
        "  path_remove: ['C:\\Visual Studio']\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "run.yaml"
    explicit.write_text(
        "prompt: hello\n"
        "model: gpt-test\n"
        "output: out\n"
        "environment:\n"
        "  unset: [LIB]\n",
        encoding="utf-8",
    )
    destination = tmp_path / "saved.yaml"
    monkeypatch.setenv(DEFAULT_CONFIG_ENV, str(defaults))

    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(explicit),
            "--save-config",
            str(destination),
            "--config-only",
            "-v",
        ],
    )

    assert result.exit_code == 0, result.output
    saved = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert saved["environment"] == {
        "unset": ["LIB"],
        "path_remove": [r"C:\Visual Studio"],
    }
    assert "Loading default configuration" in result.output


def test_config_only_saves_copy_file_paths(tmp_path: Path) -> None:
    secret = tmp_path / "servicex.yaml"
    secret.write_text("token: secret\n", encoding="utf-8")
    destination = tmp_path / "saved.yaml"
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--copy-file",
            str(secret),
            "--output",
            str(tmp_path / "out"),
            "--save-config",
            str(destination),
            "--config-only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(destination.read_text(encoding="utf-8"))["copy_files"] == [
        str(secret.resolve())
    ]


def test_config_only_saves_copy_back_paths(tmp_path: Path) -> None:
    destination = tmp_path / "saved.yaml"
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--copy-back",
            "plots/output.png",
            "--output",
            str(tmp_path / "out"),
            "--save-config",
            str(destination),
            "--config-only",
        ],
    )
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(destination.read_text(encoding="utf-8"))["copy_back"] == [
        "plots/output.png"
    ]


def test_force_maps_to_overwrite_in_saved_configuration(tmp_path: Path) -> None:
    destination = tmp_path / "saved.yaml"
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--force",
            "--save-config",
            str(destination),
            "--config-only",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert yaml.safe_load(destination.read_text(encoding="utf-8"))["overwrite"] is True


def test_config_only_requires_save_config(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--config-only",
        ],
    )
    assert result.exit_code == 2
    assert "requires --save-config" in result.output


def test_existing_result_is_rejected_before_wsl(tmp_path: Path) -> None:
    (tmp_path / "out.md").write_text("existing", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "already exists" in result.output


def test_generate_renders_yaml_to_custom_markdown_without_raw_details(tmp_path: Path) -> None:
    source = tmp_path / "result.yaml"
    source.write_text(
        yaml.safe_dump(sample_result().model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    destination = tmp_path / "summary.md"
    result = runner.invoke(
        app,
        ["generate", str(source), "--output", str(destination), "--no-details"],
    )
    assert result.exit_code == 0, result.output
    markdown = destination.read_text(encoding="utf-8")
    assert "Create hello.txt" in markdown
    assert "<summary>Workspace inventory</summary>" in markdown
    assert "<summary>Complete Codex stderr</summary>" in markdown
    assert "<summary>Model activity</summary>" in markdown
    assert "Complete Codex stdout JSONL" not in markdown


def test_generate_defaults_to_standard_diagnostics_without_raw_details(tmp_path: Path) -> None:
    source = tmp_path / "result.yaml"
    source.write_text(
        yaml.safe_dump(sample_result().model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    destination = tmp_path / "summary.md"
    result = runner.invoke(app, ["generate", str(source), "--output", str(destination)])
    assert result.exit_code == 0, result.output
    markdown = destination.read_text(encoding="utf-8")
    assert "<summary>Workspace inventory</summary>" in markdown
    assert "<summary>Complete Codex stderr</summary>" in markdown
    assert "<summary>Model activity</summary>" in markdown
    assert "Complete Codex stdout JSONL" not in markdown
