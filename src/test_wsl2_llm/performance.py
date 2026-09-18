"""Generate a self-contained, interactive summary of saved YAML results."""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path

from test_wsl2_llm.compatibility import load_result_yaml
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
    title = result.title.lstrip("# ").strip()
    question = (
        cell.question_id
        if cell
        else (title if title and title != "WSL2 Codex test result" else result.prompt)
    )
    model = cell.model_selector if cell else str(result.configuration.get("model") or "unknown")
    passed = result.run.status == "succeeded" and all(check.passed for check in result.validation)
    tokens = sum(usage.input_tokens + usage.output_tokens for usage in result.usage)
    return {
        "file": str(path),
        "question": question,
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
        "seconds": result.run.agent_execution_seconds or result.run.codex_execution_seconds,
        "started": result.run.started_at,
        "validation": [
            {"name": check.name, "passed": check.passed, "message": check.message}
            for check in result.validation
        ],
        "error": result.run.error,
        "response": result.result.final_message,
    }


def load_records(paths: Iterable[Path]) -> list[dict[str, object]]:
    return [_record(load_result_yaml(path), path) for path in paths]


def render_performance(records: list[dict[str, object]]) -> str:
    """Embed only summary fields; escape JSON before placing it in a script element."""
    data = json.dumps(records, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    page = files("test_wsl2_llm").joinpath("performance-template.html").read_text(encoding="utf-8")
    return page.replace("__RECORDS__", data)


def write_performance(source: Path, destination: Path, *, force: bool = False) -> tuple[Path, int]:
    paths = result_paths(source)
    if not paths:
        raise ValueError(f"no YAML result files found in {source}")
    if destination.exists() and not force:
        raise ValueError(f"output already exists: {destination}; use --force to replace it")
    records = load_records(paths)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_performance(records), encoding="utf-8")
    return destination, len(records)
