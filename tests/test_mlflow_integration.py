"""Token-free coverage for the MLflow batch adapter."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from test_wsl2_llm import mlflow_integration as integration
from test_wsl2_llm.cli import app
from test_wsl2_llm.models import (
    CommandResult,
    FinalResult,
    LogsResult,
    ModelInformation,
    RunResult,
    SkillsResult,
    TimingResult,
    UsageRecord,
    ValidationResult,
    WorkspaceResult,
)
from test_wsl2_llm.models import TestResult as WslTestResult
from test_wsl2_llm.provenance import build_provenance


class FakePrompt:
    name = "trial-prompt"
    version = "7"
    uri = "prompts:/trial-prompt/7"
    template = "Do {{ question }}"

    def format(self, **inputs):
        if "question" not in inputs:
            raise KeyError("question")
        return f"Do {inputs['question']}"


class FakeDataset:
    name = "trial-data"
    dataset_id = "d-123"
    digest = "abc123"

    def __init__(self, rows):
        self.rows = rows

    def to_df(self):
        return SimpleNamespace(to_dict=lambda orient: self.rows)


def row(record_id="r1", name="first", inputs=None, expectations=None):
    return {
        "dataset_record_id": record_id,
        "tags": {"name": name},
        "inputs": {"question": "something"} if inputs is None else inputs,
        "expectations": {"expected_answer": "done"} if expectations is None else expectations,
    }


def result_for(config, *, status="succeeded"):
    return WslTestResult(
        prompt=config.prompt,
        skills=SkillsResult(),
        run=RunResult(
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:02+00:00",
            total_duration_seconds=2,
            codex_execution_seconds=1,
            status=status,
            exit_code=0 if status == "succeeded" else 1,
            distro=None,
            workspace_path=None,
            workspace_retained=False,
            codex_version="test",
        ),
        timing=TimingResult(),
        configuration=config.model_dump(mode="json"),
        provenance=build_provenance(
            config, agent_version="test", target="wsl", target_version="test"
        ),
        usage=[
            UsageRecord(model=config.model, attribution="test", input_tokens=8, output_tokens=3)
        ],
        model_information=ModelInformation(pricing_file="test", currency="USD", total_cost=0.01),
        result=FinalResult(final_message="done"),
        workspace=WorkspaceResult(),
        command=CommandResult(),
        logs=LogsResult(),
        validation=[ValidationResult(name="require_string", passed=True, message="ok")],
    )


def test_mlflow_command_is_optional_and_positional():
    help_result = CliRunner().invoke(app, ["mlflow", "run", "--help"])
    assert help_result.exit_code == 0
    assert "{prompt} {dataset} {config}" in help_result.output


def test_config_ignores_local_prompt_fields_before_reading_file(tmp_path):
    path = tmp_path / "batch.yaml"
    path.write_text(
        "prompt_file: missing.txt\nprompt_template: ignored\n"
        "questions: [{id: duplicate}, {id: duplicate}]\n"
        "model: gpt-test\nmodels: [gpt-a, gpt-b]\nrepeat: 2\nthreads: 3\n",
        encoding="utf-8",
    )
    batch, shared = integration.load_batch_config(path)
    assert (batch.models, batch.repeat, batch.threads) == (["gpt-a", "gpt-b"], 2, 3)
    assert shared["model"] == "gpt-test"
    assert not integration._IGNORED_PROMPT_FIELDS.intersection(shared)


def test_dataset_selection_rendering_and_preflight_errors():
    dataset = FakeDataset([row(), row("r2", "second", {"question": "another"})])
    records = integration.prepare_records(FakePrompt(), dataset, ["second"])
    assert [(item.record_id, item.rendered_prompt) for item in records] == [("r2", "Do another")]
    selected = integration.prepare_records(
        FakePrompt(),
        FakeDataset([row(), row("r2", "second", {"unused": "not selected"})]),
        ["first"],
    )
    assert len(selected) == 1
    with pytest.raises(ValueError, match="not unique"):
        integration.prepare_records(
            FakePrompt(), FakeDataset([row(), row("r2", "first")]), ["first"]
        )
    with pytest.raises(ValueError, match="cannot fill prompt variables"):
        integration.prepare_records(FakePrompt(), FakeDataset([row(inputs={"unused": "x"})]), [])
    with pytest.raises(ValueError, match="duplicate dataset_record_id"):
        integration.prepare_records(FakePrompt(), FakeDataset([row(), row()]), [])


def test_cell_expansion_has_unique_fresh_destinations(tmp_path):
    records = integration.prepare_records(FakePrompt(), FakeDataset([row()]), [])
    batch = integration.MLflowBatchConfig(models=["gpt-a", "gpt-b"], repeat=2)
    cells = integration.prepare_cells(records, batch, {}, tmp_path / "trial")
    assert len(cells) == 4
    assert len({cell.config.output for cell in cells}) == 4
    assert all(cell.config.prompt == "Do something" for cell in cells)


def test_metrics_include_existing_validation_cost_and_usage(tmp_path):
    config = integration.build_config(
        {"model": "gpt-test"}, {"prompt": "Do this", "output": str(tmp_path / "r")}
    )
    metrics = integration._cell_metrics(result_for(config))
    assert metrics["input_tokens"] == 8
    assert metrics["output_tokens"] == 3
    assert metrics["total_cost"] == 0.01
    assert metrics["validators_passed"] == 1


def test_mlflow_url_does_not_print_embedded_credentials():
    url = integration._run_url("https://user:secret@mlflow.example/path?token=secret", "1", "abc")
    assert url == "https://mlflow.example/path/#/experiments/1/runs/abc"


def test_cancellation_and_runner_failure_do_not_launch_or_erase_reports(tmp_path, monkeypatch):
    record = integration.prepare_records(FakePrompt(), FakeDataset([row()]), [])[0]
    cell = integration.prepare_cells(
        [record],
        integration.MLflowBatchConfig(),
        {"model": "gpt-test"},
        tmp_path / "trial",
    )[0]
    console = Console()
    cancelled = integration.CancellationCoordinator()
    cancelled.cancel()
    assert "not started" in integration._run_cell(cell, cancelled, console).error

    def fail_runner(*args, **kwargs):
        raise RuntimeError("runner broke")

    monkeypatch.setattr(integration, "run_test", fail_runner)
    outcome = integration._run_cell(cell, integration.CancellationCoordinator(), console)
    assert outcome.error == "runner broke"
    assert outcome.result is None


def _server(tmp_path):
    mlflow = pytest.importorskip("mlflow")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    uri = f"http://127.0.0.1:{port}"
    backend = (tmp_path / "mlflow.db").as_posix()
    artifacts = (tmp_path / "artifacts").as_posix()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mlflow",
            "server",
            "--backend-store-uri",
            f"sqlite:///{backend}",
            "--artifacts-destination",
            artifacts,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    import requests

    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            if requests.get(f"{uri}/health", timeout=0.5).ok:
                return mlflow, proc, uri
        except requests.RequestException:
            pass
        time.sleep(0.25)
    proc.terminate()
    proc.wait(timeout=10)
    pytest.fail("MLflow SQL-backed test server did not start")


@pytest.mark.timeout(180)
def test_sql_server_tracks_prompt_dataset_traces_and_reports(tmp_path, monkeypatch):
    mlflow, proc, uri = _server(tmp_path)
    try:
        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment("integration")
        prompt = mlflow.genai.register_prompt(name="integration-prompt", template="Do {{question}}")
        dataset = mlflow.genai.datasets.create_dataset(name="integration-dataset")
        dataset.merge_records(
            [
                {
                    "inputs": {"question": "one"},
                    "expectations": {"expected_answer": "done"},
                    "tags": {"name": "one"},
                }
            ]
        )
        config = tmp_path / "batch.yaml"
        config.write_text(
            f"model: gpt-test\noutput: {tmp_path / 'results' / 'trial'}\n"
            "prompt_file: missing.txt\nquestions: [{id: ignored}]\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
        monkeypatch.setattr(
            integration,
            "run_test",
            lambda config, **kwargs: result_for(config),
        )
        code = integration.run_trial(
            "integration-prompt", "integration-dataset", config, experiment="integration"
        )
        assert code == 0
        runs = mlflow.search_runs(
            experiment_ids=[mlflow.get_experiment_by_name("integration").experiment_id]
        )
        assert len(runs) == 2
        child = runs.loc[runs["params.dataset_record_id"].notna()].iloc[0]
        assert child["params.prompt_version"] == str(prompt.version)
        assert child["params.dataset_id"] == dataset.dataset_id
        assert child["tags.provenance_identity"]
        client = mlflow.MlflowClient()
        artifacts = client.list_artifacts(child["run_id"])
        assert {item.path.split(".")[-1] for item in artifacts} == {"yaml", "md"}
        traces = mlflow.search_traces(run_id=child["run_id"], return_type="list")
        assert len(traces) == 1
        trace = traces[0]
        assert trace.data.spans[0].outputs["response"] == "done"
        names = {item.name for item in trace.info.assessments}
        assert {"expected_answer", "validator_1_require_string"} <= names

        with monkeypatch.context() as patch:
            patch.setattr(
                integration,
                "_log_cell",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("upload broke")),
            )
            assert (
                integration.run_trial(
                    "integration-prompt",
                    "integration-dataset",
                    config,
                    experiment="integration",
                )
                == 1
            )
        assert len(list((tmp_path / "results").rglob("*.yaml"))) == 2
        assert len(list((tmp_path / "results").rglob("*.md"))) == 2

        with monkeypatch.context() as patch:
            patch.setattr(
                integration,
                "run_test",
                lambda config, **kwargs: result_for(config, status="failed"),
            )
            assert (
                integration.run_trial(
                    "integration-prompt",
                    "integration-dataset",
                    config,
                    experiment="integration",
                )
                == 1
            )
        assert len(list((tmp_path / "results").rglob("*.yaml"))) == 3
    finally:
        proc.terminate()
        proc.wait(timeout=15)
