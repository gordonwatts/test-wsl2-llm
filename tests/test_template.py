from pathlib import Path
from threading import Barrier, Lock

import pytest
import yaml
from test_report import sample_result
from typer.testing import CliRunner

from test_wsl2_llm.cli import app
from test_wsl2_llm.config import output_paths
from test_wsl2_llm.template import (
    render_template,
    template_output,
    validate_questions,
)

runner = CliRunner()


def test_template_init_writes_starter_and_refuses_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "template.yaml"
    result = runner.invoke(app, ["template", "init", str(destination)])

    assert result.exit_code == 0, result.output
    content = destination.read_text(encoding="utf-8")
    assert "prompt_template: |" in content
    assert "{{ question }}" in content
    assert "questions:" in content
    assert "id: example" in content
    assert "marketplaces: []" in content
    assert "plugins: []" in content
    assert "copy_files: []" in content
    assert "repeat: 1" in content
    assert "threads: 1" in content
    assert "output: .\\results\\template" in content

    second = runner.invoke(app, ["template", "init", str(destination)])
    assert second.exit_code == 2
    assert "already exists" in second.output


def test_template_init_requires_filename() -> None:
    result = runner.invoke(app, ["template", "init"])
    assert result.exit_code == 2
    assert "Missing argument" in result.output


def test_template_render_supports_multiple_flat_fields() -> None:
    rendered = render_template(
        "{{ question }} on {{ dataset }} (all={{ include_all }})",
        {"question": "Plot ETmiss", "dataset": "ds", "include_all": True},
    )
    assert rendered == "Plot ETmiss on ds (all=true)"


def test_template_render_rejects_missing_and_unsupported_fields() -> None:
    try:
        render_template("{{ question }} {{ missing }}", {"question": "x"})
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing template field was accepted")

    try:
        render_template("{{ question.name }}", {"question": "x"})
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("nested template field was accepted")


def test_template_question_validation_rejects_duplicate_ids_and_nested_values() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        validate_questions(
            "{{ question }}",
            [{"id": "same", "question": "one"}, {"id": "same", "question": "two"}],
        )
    with pytest.raises(ValueError, match="scalar"):
        validate_questions("{{ question }}", [{"id": "one", "question": ["bad"]}])


def test_template_output_names_question_and_repetition() -> None:
    assert template_output("results/run", "etmiss", 1, 1).endswith("run-etmiss")
    assert template_output("results/run.yaml", "etmiss", 2, 3).endswith("run-etmiss-002")


def test_template_run_expands_questions_and_repetitions(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def fake_run(config, **_kwargs):
        calls.append((config.output, config.prompt))
        return sample_result()

    def fake_write(result, output, overwrite=False):
        del result, overwrite
        return output_paths(output)

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr("test_wsl2_llm.report.write_reports", fake_write)
    config = tmp_path / "batch.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "prompt_template": "Do {{ question }} for {{ dataset }}",
                "questions": [
                    {"id": "etmiss", "question": "ETmiss", "dataset": "a"},
                    {"id": "jets", "question": "jets", "dataset": "b"},
                ],
                "model": "gpt-test",
                "output": "results/run",
                "repeat": 2,
                "threads": 1,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["template", "run", str(config)])

    assert result.exit_code == 0, result.output
    assert sorted(output for output, _prompt in calls) == sorted(
        str(tmp_path / "results" / name)
        for name in ("run-etmiss-001", "run-etmiss-002", "run-jets-001", "run-jets-002")
    )
    assert {prompt for _output, prompt in calls} == {"Do ETmiss for a", "Do jets for b"}


def test_template_run_selects_questions_by_positional_id(monkeypatch, tmp_path: Path) -> None:
    prompts: list[str] = []

    def fake_run(config, **_kwargs):
        prompts.append(config.prompt)
        return sample_result()

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr(
        "test_wsl2_llm.report.write_reports",
        lambda result, output, overwrite=False: output_paths(output),
    )
    config = tmp_path / "batch.yaml"
    config.write_text(
        "prompt_template: 'Do {{ question }}'\n"
        "questions:\n"
        "  - id: q1\n    question: first\n"
        "  - id: q2\n    question: second\n"
        "  - id: q3\n    question: third\n"
        "model: test-model\noutput: results/run\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["template", "run", str(config), "q1", "q3"])

    assert result.exit_code == 0, result.output
    assert prompts == ["Do first", "Do third"]


def test_template_run_selects_questions_with_repeatable_flag_and_rejects_unknown(
    monkeypatch, tmp_path: Path
) -> None:
    prompts: list[str] = []

    def fake_run(config, **_kwargs):
        prompts.append(config.prompt)
        return sample_result()

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr(
        "test_wsl2_llm.report.write_reports",
        lambda result, output, overwrite=False: output_paths(output),
    )
    config = tmp_path / "batch.yaml"
    config.write_text(
        "prompt_template: 'Do {{ question }}'\n"
        "questions:\n  - id: q1\n    question: first\n"
        "  - id: q2\n    question: second\n"
        "model: test-model\noutput: results/run\n",
        encoding="utf-8",
    )

    selected = runner.invoke(
        app,
        ["template", "run", str(config), "--question", "q2", "--question", "q1"],
    )
    assert selected.exit_code == 0, selected.output
    assert prompts == ["Do first", "Do second"]

    unknown = runner.invoke(app, ["template", "run", str(config), "missing"])
    assert unknown.exit_code == 2
    assert "unknown question ID" in unknown.output


def test_template_run_skips_questions_with_existing_results_by_default(
    monkeypatch, tmp_path: Path
) -> None:
    prompts: list[str] = []

    def fake_run(config, **_kwargs):
        prompts.append(config.prompt)
        return sample_result()

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr(
        "test_wsl2_llm.report.write_reports",
        lambda result, output, overwrite=False: output_paths(output),
    )
    config = tmp_path / "batch.yaml"
    config.write_text(
        "prompt_template: 'Do {{ question }}'\n"
        "questions:\n  - id: q1\n    question: first\n"
        "  - id: q2\n    question: second\n"
        "model: test-model\noutput: results/run\n",
        encoding="utf-8",
    )
    existing = tmp_path / "results" / "run-q1.yaml"
    existing.parent.mkdir()
    existing.write_text("existing", encoding="utf-8")

    result = runner.invoke(app, ["template", "run", str(config)])

    assert result.exit_code == 0, result.output
    assert prompts == ["Do second"]
    assert "Skipping q1" in result.output
    assert str(existing.parent) in "".join(result.output.split())
    assert "Use --force to rerun" in result.output


def test_template_run_force_reruns_questions_with_existing_results(
    monkeypatch, tmp_path: Path
) -> None:
    prompts: list[str] = []

    def fake_run(config, **_kwargs):
        prompts.append(config.prompt)
        return sample_result()

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr(
        "test_wsl2_llm.report.write_reports",
        lambda result, output, overwrite=False: output_paths(output),
    )
    config = tmp_path / "batch.yaml"
    config.write_text(
        "prompt_template: 'Do {{ question }}'\n"
        "questions:\n  - id: q1\n    question: first\n"
        "model: test-model\noutput: results/run\noverwrite: true\n",
        encoding="utf-8",
    )
    existing = tmp_path / "results" / "run-q1.md"
    existing.parent.mkdir()
    existing.write_text("existing", encoding="utf-8")

    result = runner.invoke(app, ["template", "run", str(config), "--force"])

    assert result.exit_code == 0, result.output
    assert prompts == ["Do first"]
    assert "Rerunning q1" in result.output
    assert str(existing.parent) in "".join(result.output.split())
    assert "(--force)" in result.output


def test_template_run_accepts_saved_run_config_fields(monkeypatch, tmp_path: Path) -> None:
    captured = []

    def fake_run(config, **_kwargs):
        captured.append(config)
        return sample_result()

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr(
        "test_wsl2_llm.report.write_reports",
        lambda result, output, overwrite=False: output_paths(output),
    )
    config = tmp_path / "copied-config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "prompt": "The old single-run prompt is replaced below.",
                "prompt_template": "{{ question }}",
                "questions": [{"id": "example", "question": "new prompt"}],
                "model": "saved-model:high",
                "marketplaces": ["https://example.test/marketplace.git"],
                "plugins": ["demo@marketplace"],
                "copy_files": [],
                "copy_back": ["plot_*.png"],
                "output": "results/copied",
                "sandbox": "read-only",
                "network": False,
                "approval_policy": "never",
                "progress_lines": 3,
                "repeat": 1,
                "threads": 1,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["template", "run", str(config)])

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    resolved = captured[0]
    assert resolved.prompt == "new prompt"
    assert resolved.model == "saved-model"
    assert resolved.reasoning_effort == "high"
    assert resolved.marketplaces == ["https://example.test/marketplace.git"]
    assert resolved.plugins == ["demo@marketplace"]
    assert resolved.sandbox == "read-only"
    assert resolved.network is False
    assert resolved.approval_policy == "never"
    assert resolved.progress_lines == 3


def test_template_run_cli_overrides_and_global_threads(monkeypatch, tmp_path: Path) -> None:
    barrier = Barrier(2)
    lock = Lock()
    active = 0
    maximum = 0

    def fake_run(config, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait(timeout=2)
            return sample_result()
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr(
        "test_wsl2_llm.report.write_reports",
        lambda result, output, overwrite=False: output_paths(output),
    )
    config = tmp_path / "batch.yaml"
    config.write_text(
        "prompt_template: 'Do {{ question }}'\n"
        "questions:\n  - id: one\n    question: one\n  - id: two\n    question: two\n"
        "model: yaml-model\noutput: yaml-output\nrepeat: 1\nthreads: 1\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "template",
            "run",
            str(config),
            "--model",
            "cli-model",
            "--output",
            str(tmp_path / "cli-output"),
            "--threads",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert maximum == 2
