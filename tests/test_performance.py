"""Saved-result performance page behavior."""

import json
import re
from pathlib import Path

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
    monkeypatch.chdir(tmp_path)
    assert len(result_paths(Path("results"))) == 2
    response = runner.invoke(app, ["performance"])
    assert response.exit_code == 0, response.output
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


def test_bad_yaml_identifies_file_and_does_not_write_page(tmp_path: Path) -> None:
    source = tmp_path / "results"
    source.mkdir()
    (source / "bad.yaml").write_text("foo: [broken", encoding="utf-8")
    output = tmp_path / "performance.html"
    response = runner.invoke(app, ["performance", "--source", str(source), "--output", str(output)])
    assert response.exit_code == 2
    assert "bad.yaml" in response.output
    assert not output.exists()


def test_embedded_records_cannot_break_script_or_markup() -> None:
    html = render_performance(
        [{"question": "</script><script>alert(1)</script>", "model": "<img>"}]
    )
    assert html.count("</script>") == 2  # JSON script and application script only
    assert "\\u003c/script\\u003e" in html
    assert "<img>" not in html
