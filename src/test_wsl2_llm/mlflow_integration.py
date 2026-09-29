"""Run the existing harness against MLflow prompts and evaluation datasets."""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from test_wsl2_llm.config import (
    build_config,
    default_config_path,
    load_config_file,
    merge_config_values,
    output_stem,
)
from test_wsl2_llm.models import TestConfig, TestResult
from test_wsl2_llm.provenance import content_hash
from test_wsl2_llm.report import write_reports
from test_wsl2_llm.runner import CancellationCoordinator, run_test
from test_wsl2_llm.template import render_template, template_cell_metadata, template_output

_IGNORED_PROMPT_FIELDS = frozenset({"prompt", "prompt_file", "prompt_template", "questions"})
_UNFILLED_PROMPT_FIELD = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}")


class MLflowBatchConfig(BaseModel):
    """Batch settings shared with template runs, without local questions."""

    model_config = ConfigDict(extra="forbid")
    models: list[str] | None = Field(default=None, min_length=1)
    repeat: int = 1
    threads: int = 1

    @field_validator("repeat", "threads")
    @classmethod
    def positive_count(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("must be at least 1")
        return value


@dataclass(frozen=True)
class DatasetRecord:
    record_id: str
    name: str
    inputs: dict[str, Any]
    expectations: dict[str, Any]
    rendered_prompt: str


@dataclass(frozen=True)
class Cell:
    record: DatasetRecord
    repetition: int
    config: TestConfig


@dataclass(frozen=True)
class CellOutcome:
    cell: Cell
    result: TestResult | None
    markdown: Path | None
    yaml: Path | None
    error: str | None = None


def load_batch_config(path: Path) -> tuple[MLflowBatchConfig, dict[str, Any]]:
    """Load template settings while ignoring every competing prompt source."""
    defaults_path = default_config_path()
    defaults = (
        load_config_file(defaults_path, ignored_fields=_IGNORED_PROMPT_FIELDS)
        if defaults_path.is_file()
        else {}
    )
    values = merge_config_values(
        defaults,
        load_config_file(path, ignored_fields=_IGNORED_PROMPT_FIELDS),
    )
    for field in _IGNORED_PROMPT_FIELDS:
        values.pop(field, None)
    batch_values = {
        name: values.pop(name) for name in ("models", "repeat", "threads") if name in values
    }
    return MLflowBatchConfig.model_validate(batch_values), values


def prepare_records(
    prompt: Any, dataset: Any, selected_questions: list[str]
) -> list[DatasetRecord]:
    """Validate all records and render the one resolved prompt before launching jobs."""
    rows = dataset.to_df().to_dict("records")
    if not rows:
        raise ValueError(f"MLflow dataset {dataset.name!r} is empty")
    raw_records: list[tuple[str, str, dict[str, Any]]] = []
    ids: set[str] = set()
    names: dict[str, int] = {}
    for row in rows:
        record_id = row.get("dataset_record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("each MLflow dataset row needs a dataset_record_id")
        if record_id in ids:
            raise ValueError(f"duplicate dataset_record_id: {record_id}")
        ids.add(record_id)
        tags = row.get("tags") or {}
        if not isinstance(tags, dict):
            raise ValueError(f"record {record_id} has invalid tags")
        name = str(tags.get("name") or record_id)
        names[name] = names.get(name, 0) + 1
        raw_records.append((record_id, name, row))
    selected_ids = ids
    if selected_questions:
        if len(set(selected_questions)) != len(selected_questions):
            raise ValueError("--question selectors may not be repeated")
        selected_ids = set()
        for selector in selected_questions:
            if selector in ids:
                selected_ids.add(selector)
            elif names.get(selector) == 1:
                selected_ids.add(next(rid for rid, name, _ in raw_records if name == selector))
            elif names.get(selector, 0) > 1:
                raise ValueError(f"question name {selector!r} is not unique; use record ID")
            else:
                raise ValueError(f"unknown question {selector!r}")
    records: list[DatasetRecord] = []
    for record_id, name, row in raw_records:
        if record_id not in selected_ids:
            continue
        inputs = row.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError(f"record {record_id} needs an inputs mapping")
        expectations = row.get("expectations") or {}
        if not isinstance(expectations, dict):
            raise ValueError(f"record {record_id} has invalid expectations")
        rendered = render_registered_prompt(prompt, inputs, record_id)
        records.append(DatasetRecord(record_id, name, inputs, expectations, rendered))
    return records


def render_registered_prompt(prompt: Any, inputs: dict[str, Any], record_id: str) -> str:
    """Render a registered text prompt, rejecting leftover simple variables."""
    try:
        rendered = prompt.format(**inputs)
    except Exception as exc:
        # Keep a compatible fallback for simple {{ field }} prompt templates.
        template = getattr(prompt, "template", None)
        if not isinstance(template, str):
            raise ValueError(f"record {record_id} cannot fill prompt variables: {exc}") from exc
        try:
            rendered = render_template(template, inputs, record_id)
        except ValueError as fallback_exc:
            raise ValueError(
                f"record {record_id} cannot fill prompt variables: {fallback_exc}"
            ) from exc
    if isinstance(rendered, str) and _UNFILLED_PROMPT_FIELD.search(rendered):
        template = getattr(prompt, "template", None)
        if isinstance(template, str):
            rendered = render_template(template, inputs, record_id)
    if not isinstance(rendered, str) or not rendered.strip():
        raise ValueError(f"record {record_id} renders to an empty prompt")
    return rendered


def inspect_prompt_dataset(
    prompt_name: str,
    dataset_name: str,
    *,
    console: Console | None = None,
) -> None:
    """Fetch and display the resolved prompt and every raw evaluation record."""
    try:
        import mlflow
    except ImportError as exc:
        raise ValueError("install the MLflow extra: pip install 'test-wsl2-llm[mlflow]'") from exc
    from mlflow import MlflowClient
    from mlflow.genai.datasets import get_dataset

    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not tracking_uri:
        raise ValueError("MLFLOW_TRACKING_URI is required")
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    client.search_experiments(max_results=1)
    prompt = mlflow.genai.load_prompt(f"prompts:/{prompt_name}@latest")
    dataset = get_dataset(name=dataset_name)
    rows = dataset.to_df().to_dict("records")
    console = console or Console()

    template = prompt.template
    template_text = template if isinstance(template, str) else json.dumps(template, indent=2)
    console.print(
        Panel(
            Text(template_text, no_wrap=False),
            title=f"Prompt {prompt.name} · version {prompt.version}",
            subtitle=prompt.uri,
            expand=False,
        )
    )
    console.print(
        Panel(
            Text(
                f"Name: {dataset.name}\nID: {dataset.dataset_id}\n"
                f"Digest: {dataset.digest}\nRecords: {len(rows)}"
            ),
            title="Evaluation dataset",
            expand=False,
        )
    )
    for index, row in enumerate(rows, start=1):
        record_id = str(row.get("dataset_record_id") or f"row-{index}")
        tags = row.get("tags") or {}
        name = tags.get("name", record_id) if isinstance(tags, dict) else record_id
        raw_record = json.dumps(row, ensure_ascii=False, indent=2, default=str)
        console.print(
            Panel(
                Text(raw_record, no_wrap=False),
                title=f"Record {record_id} · {name}",
                expand=False,
            )
        )
        inputs = row.get("inputs")
        if not isinstance(inputs, dict):
            console.print("[yellow]Cannot render this record: inputs is not a mapping.[/yellow]")
            continue
        try:
            rendered = render_registered_prompt(prompt, inputs, record_id)
        except Exception as exc:
            console.print(f"[red]Prompt rendering failed for {record_id}:[/red] {exc}")
        else:
            console.print(
                Panel(Text(rendered, no_wrap=False), title="Rendered prompt", expand=False)
            )


def prepare_cells(
    records: list[DatasetRecord],
    batch: MLflowBatchConfig,
    shared: dict[str, Any],
    trial_dir: Path,
) -> list[Cell]:
    """Expand record × model × repetition using the existing TestConfig contract."""
    selectors = batch.models or [shared.get("model")]
    if any(not isinstance(selector, str) or not selector.strip() for selector in selectors):
        raise ValueError("MLflow batch configuration needs model or models")
    cells: list[Cell] = []
    model_ids: set[str] = set()
    outputs: set[str] = set()
    for selector in selectors:
        for record in records:
            for repetition in range(1, batch.repeat + 1):
                output = template_output(
                    str(trial_dir / "result"),
                    record.record_id,
                    repetition,
                    batch.repeat,
                    selector,
                )
                config = build_config(
                    shared,
                    {
                        "prompt": record.rendered_prompt,
                        "model": selector,
                        "output": output,
                        "overwrite": False,
                    },
                )
                model_ids.add(config.model_selector.casefold())
                key = config.output.casefold()
                if key in outputs:
                    raise ValueError(f"duplicate result destination: {config.output}")
                outputs.add(key)
                cells.append(Cell(record, repetition, config))
    if len(model_ids) != len(selectors):
        raise ValueError("model selectors must be unique")
    return cells


def _run_cell(cell: Cell, coordinator: CancellationCoordinator, console: Console) -> CellOutcome:
    job_id = f"mlflow-{cell.record.record_id}-{cell.config.model_selector}-{cell.repetition}"
    if not coordinator.claim(job_id):
        return CellOutcome(cell, None, None, None, "not started: cancellation requested")

    def persist(result: TestResult) -> None:
        result.template_cell = template_cell_metadata(
            cell.record.record_id, cell.repetition, cell.config
        )
        write_reports(result, cell.config.output)

    try:
        result = run_test(
            cell.config,
            console=console,
            live_progress=False,
            invocation=sys.argv,
            report_callback=persist,
            cancellation=coordinator,
            job_id=job_id,
        )
        result.template_cell = template_cell_metadata(
            cell.record.record_id, cell.repetition, cell.config
        )
        markdown, yaml = write_reports(result, cell.config.output, True)
        return CellOutcome(cell, result, markdown, yaml)
    except Exception as exc:
        return CellOutcome(cell, None, None, None, str(exc) or exc.__class__.__name__)


def _cell_metrics(result: TestResult) -> dict[str, float]:
    usage = result.usage
    metrics: dict[str, float] = {
        "succeeded": float(result.run.status == "succeeded"),
        "exit_code": float(result.run.exit_code),
        "total_duration_seconds": result.run.total_duration_seconds,
        "agent_execution_seconds": (
            result.run.agent_execution_seconds
            if result.run.agent_execution_seconds is not None
            else result.run.codex_execution_seconds
        ),
        "input_tokens": float(sum(item.input_tokens for item in usage)),
        "cached_input_tokens": float(sum(item.cached_input_tokens for item in usage)),
        "output_tokens": float(sum(item.output_tokens for item in usage)),
        "validators_passed": float(sum(item.passed for item in result.validation)),
        "validators_failed": float(sum(not item.passed for item in result.validation)),
    }
    if result.model_information.total_cost is not None:
        metrics["total_cost"] = result.model_information.total_cost
    return metrics


def _log_cell(
    mlflow: Any,
    client: Any,
    prompt: Any,
    dataset: Any,
    parent_id: str,
    experiment_id: str,
    outcome: CellOutcome,
) -> None:
    """Publish one completed local cell from the coordinator thread."""
    from mlflow.entities import SpanStatusCode, SpanType

    cell = outcome.cell
    result = outcome.result
    with mlflow.start_run(run_name=cell.record.name, nested=True) as child:
        client.link_prompt_version_to_run(child.info.run_id, prompt)
        mlflow.log_params(
            {
                "parent_run_id": parent_id,
                "prompt_name": prompt.name,
                "prompt_version": str(prompt.version),
                "prompt_uri": prompt.uri,
                "dataset_name": dataset.name,
                "dataset_id": dataset.dataset_id,
                "dataset_digest": dataset.digest,
                "dataset_record_id": cell.record.record_id,
                "model": cell.config.model_selector,
                "repetition": cell.repetition,
            }
        )
        status = result.run.status if result is not None else "failed"
        mlflow.set_tags({"status": status, "question": cell.record.name})
        if result is not None:
            mlflow.log_metrics(_cell_metrics(result))
            mlflow.set_tag("cost_currency", result.model_information.currency)
            if result.provenance is not None:
                mlflow.set_tags(
                    {
                        "configuration_hash": result.provenance.configuration_hash,
                        "provenance_identity": result.provenance.identity,
                    }
                )
            assert outcome.markdown is not None and outcome.yaml is not None
            mlflow.log_artifact(str(outcome.markdown))
            mlflow.log_artifact(str(outcome.yaml))
        elif outcome.error:
            mlflow.set_tag("execution_error", outcome.error[:500])

        start_ns = (
            int(datetime.fromisoformat(result.run.started_at).timestamp() * 1_000_000_000)
            if result is not None
            else None
        )
        end_ns = (
            int(datetime.fromisoformat(result.run.finished_at).timestamp() * 1_000_000_000)
            if result is not None
            else None
        )
        span = mlflow.start_span_no_context(
            name="harness_run",
            span_type=SpanType.AGENT,
            experiment_id=experiment_id,
            inputs={
                "dataset_inputs": cell.record.inputs,
                "prompt": cell.record.rendered_prompt,
            },
            tags={
                "parent_run_id": parent_id,
                "child_run_id": child.info.run_id,
                "dataset_record_id": cell.record.record_id,
                "prompt_version": str(prompt.version),
            },
            start_time_ns=start_ns,
        )
        span.end(
            outputs={"response": result.result.final_message if result is not None else None},
            status=SpanStatusCode.OK if status == "succeeded" else SpanStatusCode.ERROR,
            end_time_ns=end_ns,
        )
        # Trace export is asynchronous; the server must have the trace before
        # prompt links and assessments can refer to its ID.
        mlflow.flush_trace_async_logging()
        client.link_traces_to_run([span.trace_id], child.info.run_id)
        client.link_prompt_versions_to_trace([prompt], span.trace_id)
        for name, expected in cell.record.expectations.items():
            mlflow.log_expectation(trace_id=span.trace_id, name=name, value=expected)
        if result is not None:
            for index, check in enumerate(result.validation, 1):
                mlflow.log_feedback(
                    trace_id=span.trace_id,
                    name=f"validator_{index}_{check.name}",
                    value=check.passed,
                    rationale=check.message,
                )


def _run_url(tracking_uri: str, experiment_id: str, run_id: str) -> str | None:
    if tracking_uri.startswith(("http://", "https://")):
        parts = urlsplit(tracking_uri)
        safe = urlunsplit(
            parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1], query="", fragment="")
        )
        return f"{safe.rstrip('/')}/#/experiments/{experiment_id}/runs/{run_id}"
    return None


def run_trial(
    prompt_name: str,
    dataset_name: str,
    config_path: Path,
    *,
    experiment: str = "test-wsl2-llm",
    questions: list[str] | None = None,
    console: Console | None = None,
) -> int:
    """Run a fresh batch, preserving local reports even if upload fails."""
    try:
        import mlflow
    except ImportError as exc:
        raise ValueError("install the MLflow extra: pip install 'test-wsl2-llm[mlflow]'") from exc
    console = console or Console(stderr=True)
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not tracking_uri:
        raise ValueError("MLFLOW_TRACKING_URI is required")
    from mlflow import MlflowClient
    from mlflow.genai.datasets import get_dataset

    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    client.search_experiments(max_results=1)
    batch, shared = load_batch_config(config_path)
    prompt = mlflow.genai.load_prompt(f"prompts:/{prompt_name}@latest")
    dataset = get_dataset(name=dataset_name)
    records = prepare_records(prompt, dataset, questions or [])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = output_stem(shared.pop("output", None) or str(Path.cwd() / "results" / "mlflow"))
    trial_dir = base.parent / f"{base.name}-{stamp}-{uuid4().hex[:8]}"
    cells = prepare_cells(records, batch, shared, trial_dir)
    if not cells:
        raise ValueError("no MLflow cells to run")
    mlflow.set_experiment(experiment)
    trial_dir.mkdir(parents=True, exist_ok=False)

    successful = 0
    processed = 0
    uploaded = 0
    total_cost = 0.0
    total_duration = 0.0
    interrupted = False
    upload_errors: list[str] = []
    with mlflow.start_run(run_name=f"{prompt.name}-v{prompt.version}-{stamp}") as parent:
        parent_id = parent.info.run_id
        client.link_prompt_version_to_run(parent_id, prompt)
        mlflow.log_params(
            {
                "prompt_name": prompt.name,
                "prompt_version": str(prompt.version),
                "prompt_uri": prompt.uri,
                "prompt_sha256": content_hash(prompt.template.encode("utf-8")),
                "dataset_name": dataset.name,
                "dataset_id": dataset.dataset_id,
                "dataset_digest": dataset.digest,
                "num_cells": len(cells),
            }
        )
        mlflow.log_text(prompt.template, "prompt_template.txt")
        coordinator = CancellationCoordinator()
        executor = ThreadPoolExecutor(max_workers=min(batch.threads, len(cells)))
        futures: dict[Future[CellOutcome], Cell] = {
            executor.submit(_run_cell, cell, coordinator, console): cell for cell in cells
        }
        try:
            for future in as_completed(futures):
                outcome = future.result()
                processed += 1
                if outcome.result is not None:
                    total_cost += outcome.result.model_information.total_cost or 0.0
                    total_duration += outcome.result.run.total_duration_seconds
                try:
                    _log_cell(
                        mlflow,
                        client,
                        prompt,
                        dataset,
                        parent_id,
                        parent.info.experiment_id,
                        outcome,
                    )
                    uploaded += 1
                    if outcome.result is not None and outcome.result.run.status == "succeeded":
                        successful += 1
                except Exception as exc:
                    upload_errors.append(
                        f"{outcome.cell.record.record_id}: {str(exc) or exc.__class__.__name__}"
                    )
        except KeyboardInterrupt:
            interrupted = True
            coordinator.cancel()
            for future in futures:
                future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=interrupted)
        failed = len(cells) - successful
        mlflow.log_metrics(
            {
                "processed_cells": processed,
                "uploaded_cells": uploaded,
                "completed_cells": successful,
                "failed_cells": failed,
                "upload_failures": len(upload_errors),
                "total_cost": total_cost,
                "total_duration_seconds": total_duration,
            }
        )
        mlflow.set_tag("status", "failure" if failed or upload_errors or interrupted else "success")
        url = _run_url(tracking_uri, parent.info.experiment_id, parent_id)
        console.print(f"MLflow run: {parent_id}")
        if url:
            console.print(f"MLflow UI: {url}")
        console.print(f"Local results: {trial_dir.resolve()}")
        console.print(
            f"Completed: {successful}/{len(cells)}; failed: {failed}; uploaded: {uploaded}"
        )
        for error in upload_errors:
            console.print(f"[red]Upload failed:[/red] {error}")
    return 130 if interrupted else 1 if failed or upload_errors else 0
