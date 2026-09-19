"""Generate a self-contained, interactive summary of saved YAML results."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path

import yaml

from test_wsl2_llm.compatibility import load_result_values
from test_wsl2_llm.models import TestResult


def result_paths(source: Path) -> list[Path]:
    """Find all result YAML files below a directory, or use one explicit file."""
    if source.is_file():
        if source.suffix.lower() not in {".yaml", ".yml"}:
            raise ValueError(f"expected a YAML result file: {source}")
        return [source]
    if not source.is_dir():
        raise ValueError(f"result source does not exist or is not a directory: {source}")
    return sorted(
        (
            path
            for path in source.rglob("*")
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
        ),
        key=lambda path: str(path).casefold(),
    )


def _record(result: TestResult, path: Path) -> dict[str, object]:
    cell = result.template_cell
    title_id = re.match(r"^#?\s*Question:\s*(\S+)", result.title, re.IGNORECASE)
    # Standalone output names may end with a three-digit trial index.
    stem_id = re.sub(r"-\d{3}$", "", path.stem)
    question = cell.question_id if cell else (title_id.group(1) if title_id else stem_id)
    model = cell.model_selector if cell else str(result.configuration.get("model") or "unknown")
    directory = path.parent.name
    passed = result.run.status == "succeeded" and all(check.passed for check in result.validation)
    tokens = sum(usage.input_tokens + usage.output_tokens for usage in result.usage)
    return {
        "file": str(path),
        "directory": directory,
        "question": question,
        "question_label": f"{directory} / {question}",
        "prompt": result.prompt,
        "model": model,
        "agent": result.run.agent,
        "target": result.run.target,
        "trial": cell.repetition if cell else None,
        "passed": passed,
        "status": result.run.status,
        "cost": result.model_information.total_cost,
        "currency": result.model_information.currency,
        "tokens": tokens,
        "seconds": (
            result.run.agent_execution_seconds
            if result.run.agent_execution_seconds is not None
            else result.run.codex_execution_seconds
        ),
        "started": result.run.started_at,
        "validation": [
            {"name": check.name, "passed": check.passed, "message": check.message}
            for check in result.validation
        ],
        "error": result.run.error,
        "response": result.result.final_message,
    }


def _looks_like_result(values: object) -> bool:
    """Distinguish harness results from YAML artifacts copied beside them."""
    if not isinstance(values, dict):
        return True  # A scalar/list may be a broken result; report the schema error.
    if values.get("schema_version") == 2:
        return True
    markers = {
        "prompt",
        "run",
        "timing",
        "configuration",
        "usage",
        "model_information",
        "result",
        "logs",
        "workspace",
        "validation",
        "template_cell",
    }
    present = markers.intersection(values)
    return len(present) >= 2 and bool(
        present.intersection({"run", "model_information", "result", "logs"})
    )


def load_records(
    paths: Iterable[Path], *, skipped: list[Path] | None = None
) -> list[dict[str, object]]:
    records = []
    for path in paths:
        try:
            values = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"while trying to parse file '{path}' as YAML: {exc}") from exc
        if not _looks_like_result(values):
            if skipped is not None:
                skipped.append(path)
            continue
        result = load_result_values(values, source=str(path))
        records.append(_record(result, path))
    currencies = {str(record["currency"]) for record in records if record["cost"] is not None}
    if len(currencies) > 1:
        listed = ", ".join(sorted(currencies))
        raise ValueError(f"cannot combine priced results in different currencies: {listed}")
    return records


def render_performance(records: list[dict[str, object]]) -> str:
    """Embed only summary fields; escape JSON before placing it in a script element."""
    data = json.dumps(records, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    page = files("test_wsl2_llm").joinpath("performance-template.html").read_text(encoding="utf-8")
    return page.replace("__RECORDS__", data)


def write_performance(
    sources: Iterable[Path], destination: Path, *, force: bool = False
) -> tuple[Path, int, list[Path]]:
    sources = list(sources)
    paths = list(
        dict.fromkeys(path.resolve() for source in sources for path in result_paths(source))
    )
    if not paths:
        raise ValueError(f"no YAML files found in {', '.join(map(str, sources))}")
    if destination.exists() and not force:
        raise ValueError(f"output already exists: {destination}; use --force to replace it")
    skipped: list[Path] = []
    records = load_records(paths, skipped=skipped)
    if not records:
        raise ValueError(
            "no result YAML files found in selected sources; "
            f"skipped {len(skipped)} non-result YAML files"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_performance(records), encoding="utf-8")
    return destination, len(records), skipped
