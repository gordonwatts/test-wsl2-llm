from pathlib import Path
from threading import Barrier, Lock

import pytest
import yaml
from test_report import sample_result
from typer.testing import CliRunner

from test_wsl2_llm.cli import app
from test_wsl2_llm.config import output_paths
from test_wsl2_llm.template import (
    question_copy_back,
    question_title,
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


def test_question_copy_back_adds_and_removes_shared_patterns() -> None:
    assert question_copy_back(
        ["plot_*.png", "ab-output.root"],
        {"copy_back": ["script.py", "-ab-output.root", "script.py"]},
    ) == ["plot_*.png", "script.py"]


def test_template_question_validation_accepts_copy_back_list() -> None:
    validate_questions(
        "{{ question }}",
        [{"id": "one", "question": "first", "copy_back": ["plot.png", "-default.root"]}],
        shared_copy_back=["default.root"],
    )


def test_question_copy_back_rejects_unknown_removal() -> None:
    with pytest.raises(ValueError, match="no preceding pattern"):
        question_copy_back([], {"id": "one", "copy_back": ["-missing.root"]})


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
        for name in (
            "run-etmiss-gpt-test-medium-001",
            "run-etmiss-gpt-test-medium-002",
            "run-jets-gpt-test-medium-001",
            "run-jets-gpt-test-medium-002",
        )
    )
    assert {prompt for _output, prompt in calls} == {"Do ETmiss for a", "Do jets for b"}


def test_template_run_selects_questions_by_positional_id(monkeypatch, tmp_path: Path) -> None:
    prompts: list[str] = []

    def fake_run(config, **_kwargs):
        prompts.append(config.prompt)
        return sample_result()

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
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
    config = tmp_path / "batch.yaml"
    config.write_text(
        "prompt_template: 'Do {{ question }}'\n"
        "questions:\n  - id: q1\n    question: first\n"
        "  - id: q2\n    question: second\n"
        "model: test-model\noutput: results/run\n",
        encoding="utf-8",
    )
    existing = tmp_path / "results" / "run-q1-test-model-medium.yaml"
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
    config = tmp_path / "batch.yaml"
    config.write_text(
        "prompt_template: 'Do {{ question }}'\n"
        "questions:\n  - id: q1\n    question: first\n"
        "model: test-model\noutput: results/run\noverwrite: true\n",
        encoding="utf-8",
    )
    existing = tmp_path / "results" / "run-q1-test-model-medium.md"
    existing.parent.mkdir()
    existing.write_text("existing", encoding="utf-8")

    result = runner.invoke(app, ["template", "run", str(config), "--force", "-v"])

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


@pytest.mark.parametrize(
    ("question", "prompt", "excerpt"),
    [
        (
            {"question": "123456789012345678901234567890EXTRA"},
            "wrapper",
            "123456789012345678901234567890",
        ),
        ({"question": "First line\nsecond\tline"}, "wrapper", "First line second line"),
        ({"quantity": "jets"}, "Plot jets", "Plot jets"),
    ],
)
def test_question_title_excerpt(question, prompt, excerpt):
    assert question_title("q1", question, prompt) == f"# Question: q1 - {excerpt}..."


def test_template_report_writes_question_title_and_honors_overrides(monkeypatch, tmp_path):
    def fake_run(config, **_kwargs):
        result = sample_result()
        result.title = config.title
        result.configuration = config.model_dump(mode="json")
        return result

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    config = tmp_path / "titles.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "prompt_template": "Do {{ question }}",
                "questions": [{"id": "q1", "question": "First question"}],
                "model": "test:medium",
                "output": "results/title",
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["template", "run", str(config)])
    assert result.exit_code == 0, result.output
    stem = template_output(str(tmp_path / "results/title"), "q1", 1, 1, "test:medium")
    markdown, report = output_paths(stem)
    assert (
        markdown.read_text(encoding="utf-8").splitlines()[0] == "# Question: q1 - First question..."
    )
    saved = yaml.safe_load(report.read_text(encoding="utf-8"))
    assert saved["title"] == "# Question: q1 - First question..."


@pytest.mark.parametrize("from_cli", [False, True])
def test_model_matrix_expands_and_resumes_individual_cells(monkeypatch, tmp_path, from_cli):
    calls = []
    written = []

    def fake_run(config, **_kwargs):
        calls.append(config)
        return sample_result()

    def fake_write(result, output, overwrite=False):
        paths = output_paths(output)
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("completed", encoding="utf-8")
        written.append((paths, overwrite))
        return paths

    monkeypatch.setattr("test_wsl2_llm.runner.run_test", fake_run)
    monkeypatch.setattr("test_wsl2_llm.report.write_reports", fake_write)
    selectors = ["gpt-5.4:high", "gpt-5.4:low", "gpt-5.5:high"]
    values = {
        "prompt_template": "Do {{ question }}",
        "questions": [{"id": "q1", "question": "first"}, {"id": "q2", "question": "second"}],
        "output": "results/run",
        "repeat": 2,
        "models": ["unused:medium"] if from_cli else selectors,
    }
    config = tmp_path / "matrix.yaml"
    config.write_text(yaml.safe_dump(values), encoding="utf-8")
    args = ["template", "run", str(config)]
    if from_cli:
        for selector in selectors:
            args.extend(["--model", selector])
    existing = output_paths(
        template_output(str(tmp_path / "results/run"), "q1", 1, 2, selectors[0])
    )[1]
    existing.parent.mkdir()
    existing.write_text("already completed", encoding="utf-8")

    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    expected = {
        (selector, prompt, repetition)
        for selector in selectors
        for prompt in ["Do first", "Do second"]
        for repetition in [1, 2]
    } - {(selectors[0], "Do first", 1)}
    assert {(c.model_selector, c.prompt, int(c.output[-3:])) for c in calls} == expected
    assert len({paths[0] for paths, _ in written}) == 11
    assert all(overwrite for _, overwrite in written)
    assert existing.read_text(encoding="utf-8") == "already completed"
    calls.clear()
    resumed = runner.invoke(app, args)
    assert resumed.exit_code == 0, resumed.output
    assert not calls
    assert "No questions to run" in resumed.output

    forced = runner.invoke(app, [*args, "--force"])
    assert forced.exit_code == 0, forced.output
    assert len(calls) == 12
    assert all(overwrite for _, overwrite in written[-12:])


def test_matrix_save_config_round_trip_and_single_model_override(monkeypatch, tmp_path):
    config = tmp_path / "matrix.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "prompt_template": "{{ question }}",
                "questions": [{"id": "q1", "question": "first"}],
                "model": "legacy:low",
                "models": ["test:high", "test:low"],
                "output": "results/run",
            }
        ),
        encoding="utf-8",
    )
    saved = tmp_path / "saved.yaml"
    result = runner.invoke(
        app,
        [
            "template",
            "run",
            str(config),
            "--save-config",
            str(saved),
            "--config-only",
        ],
    )
    assert result.exit_code == 0, result.output
    values = yaml.safe_load(saved.read_text(encoding="utf-8"))
    assert values["models"] == ["test:high", "test:low"]
    assert "model" not in values
    calls = []
    monkeypatch.setattr(
        "test_wsl2_llm.runner.run_test", lambda c, **kw: calls.append(c) or sample_result()
    )
    result = runner.invoke(app, ["template", "run", str(saved)])
    assert result.exit_code == 0, result.output
    assert [c.model_selector for c in calls] == ["test:high", "test:low"]
    calls.clear()
    result = runner.invoke(app, ["template", "run", str(saved), "--model", "override:xhigh"])
    assert result.exit_code == 0, result.output
    assert [c.model_selector for c in calls] == ["override:xhigh"]


@pytest.mark.parametrize("models", [[], [""], ["test:invalid"], ["test", "test:medium"]])
def test_matrix_rejects_invalid_models_before_execution(monkeypatch, tmp_path, models):
    calls = []
    monkeypatch.setattr("test_wsl2_llm.runner.run_test", lambda *a, **kw: calls.append(a))
    config = tmp_path / "invalid.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "prompt_template": "{{ question }}",
                "questions": [{"id": "q1", "question": "first"}],
                "models": models,
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["template", "run", str(config)])
    assert result.exit_code == 2, result.output
    assert not calls


def test_template_names_keep_entire_selector_and_dotted_question_id():
    stems = [
        template_output("run.v1", "q.1", 1, 1, selector)
        for selector in [
            "gpt-5.4:high",
            "gpt-5.4:low",
            "gpt-5.5:high",
            "org/model:high",
            "org%2Fmodel:high",
        ]
    ]
    paths = [output_paths(stem)[0] for stem in stems]
    assert len(set(paths)) == len(stems)
    assert all(path.stem == stem for path, stem in zip(paths, stems, strict=True))
    assert "gpt-5-4-high" in stems[0]
    assert all("%" not in stem and ":" not in stem for stem in stems)
