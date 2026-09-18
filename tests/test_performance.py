"""Saved-result performance page behavior."""

import json
import re
from pathlib import Path

import pytest
import yaml
from test_report import sample_result
from typer.testing import CliRunner

from test_wsl2_llm.cli import app
from test_wsl2_llm.models import TemplateCell, ValidationResult
from test_wsl2_llm.performance import load_records, render_performance, result_paths

runner = CliRunner()


def _save(path: Path, *, model: str, question: str, passed: bool, cost: float | None) -> None:
    result = sample_result().model_copy(deep=True)
    result.template_cell = TemplateCell(
        question_id=question, model_selector=model, repetition=1, fingerprint="abc"
    )
    result.configuration.model = model
    result.validation = [ValidationResult(name="check", passed=passed, message="checked")]
    result.model_information.total_cost = cost
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(result.model_dump(mode="json")), encoding="utf-8")


def test_recursive_default_cli_and_explicit_file(tmp_path: Path, monkeypatch) -> None:
    _save(
        tmp_path / "results" / "batch" / "one.yaml",
        model="model-a",
        question="q1",
        passed=True,
        cost=0.2,
    )
    _save(
        tmp_path / "results" / "batch" / "two.yml",
        model="model-b",
        question="q1",
        passed=False,
        cost=None,
    )
    (tmp_path / "results" / "batch" / "one.ab-config.yaml").write_text(
        "CommonServices:\n  runSystematics: false\nJets:\n  - containerName: AnaJets\n",
        encoding="utf-8",
    )
    (tmp_path / "results" / "batch" / "two.datasets.yaml").write_text(
        "$schema: ./datasets.schema.json\n"
        "schema_version: '0.2.0'\n"
        "analysis:\n  name: test\n"
        "datasets: {}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert len(result_paths(Path("results"))) == 4
    response = runner.invoke(app, ["performance"])
    assert response.exit_code == 0, response.output
    assert "Skipped 2 non-result YAML file(s)" in response.output
    assert "one.ab-config.yaml" in response.output
    assert "two.datasets.yaml" in response.output
    page = (tmp_path / "performance.html").read_text(encoding="utf-8")
    data = json.loads(
        re.search(r'<script id="records" type="application/json">(.*?)</script>', page, re.S).group(
            1
        )
    )
    assert len(data) == 2
    assert [(row["model"], row["passed"], row["cost"]) for row in data] == [
        ("model-a", True, 0.2),
        ("model-b", False, None),
    ]
    assert data[0]["tokens"] == 12
    assert all(
        name in page
        for name in (
            "Cost versus success",
            "Model summary",
            "Pass fraction by question",
            "Average tokens per trial",
            "Trial explorer",
        )
    )
    assert runner.invoke(app, ["performance"]).exit_code == 2
    assert runner.invoke(app, ["performance", "--force"]).exit_code == 0
    assert (
        runner.invoke(
            app, ["performance", "--source", "results/batch/one.yaml", "--output", "one.html"]
        ).exit_code
        == 0
    )
    assert len(load_records(result_paths(Path("results/batch/one.yaml")))) == 1


def test_same_question_and_model_in_two_directories_stay_distinct(tmp_path: Path) -> None:
    root = tmp_path / "results"
    _save(root / "batch-a" / "trial.yaml", model="model-a", question="q1", passed=True, cost=0.1)
    _save(root / "batch-b" / "trial.yaml", model="model-a", question="q1", passed=False, cost=0.2)

    records = load_records(result_paths(root))
    assert [record["directory"] for record in records] == ["batch-a", "batch-b"]
    assert [record["question"] for record in records] == ["q1", "q1"]
    assert [record["question_label"] for record in records] == ["batch-a / q1", "batch-b / q1"]
    page = render_performance(records)
    assert '<select id="directory">' in page
    assert "<th>Directory</th><th>Question</th>" in page


def test_bad_yaml_identifies_file_and_does_not_write_page(tmp_path: Path) -> None:
    source = tmp_path / "results"
    source.mkdir()
    (source / "bad.yaml").write_text("foo: [broken", encoding="utf-8")
    output = tmp_path / "performance.html"
    response = runner.invoke(app, ["performance", "--source", str(source), "--output", str(output)])
    assert response.exit_code == 2
    assert "bad.yaml" in response.output
    assert not output.exists()


@pytest.mark.parametrize(
    "document",
    ["schema_version: 2\nprompt: hello\n", "schema_version: 99\nprompt: hello\nrun: {}\n"],
)
def test_incomplete_or_future_result_is_not_skipped(tmp_path: Path, document: str) -> None:
    source = tmp_path / "broken-result.yaml"
    source.write_text(document, encoding="utf-8")
    response = runner.invoke(
        app, ["performance", "--source", str(source), "--output", str(tmp_path / "out.html")]
    )
    assert response.exit_code == 2
    assert "broken-result.yaml" in re.sub(r"\s+", "", response.output)
    assert not (tmp_path / "out.html").exists()


def test_standalone_result_uses_saved_model_prompt_and_zero_execution_time(tmp_path: Path) -> None:
    result = sample_result().model_copy(deep=True)
    result.configuration.model = "standalone-model:medium"
    result.run.agent_execution_seconds = 0.0
    source = tmp_path / "standalone.yaml"
    source.write_text(yaml.safe_dump(result.model_dump(mode="json")), encoding="utf-8")

    record = load_records([source])[0]
    assert record["model"] == "standalone-model:medium"
    assert record["question"] == result.prompt
    assert record["seconds"] == 0.0
    assert record["passed"] is True


def test_mixed_priced_currencies_are_rejected(tmp_path: Path) -> None:
    usd = tmp_path / "usd.yaml"
    eur = tmp_path / "eur.yaml"
    _save(usd, model="a", question="q1", passed=True, cost=0.1)
    result = sample_result().model_copy(deep=True)
    result.model_information.currency = "EUR"
    result.model_information.total_cost = 0.2
    eur.write_text(yaml.safe_dump(result.model_dump(mode="json")), encoding="utf-8")

    with pytest.raises(ValueError, match="different currencies: EUR, USD"):
        load_records([usd, eur])


def test_embedded_records_cannot_break_script_or_markup() -> None:
    html = render_performance(
        [{"question": "</script><script>alert(1)</script>", "model": "<img>"}]
    )
    assert html.count("</script>") == 2  # JSON script and application script only
    assert "\\u003c/script\\u003e" in html
    assert "<img>" not in html
