"""Serialize paired machine-readable and human-readable run reports."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from test_wsl2_llm.config import output_paths
from test_wsl2_llm.models import TestResult


def write_reports(result: TestResult, output: str, overwrite: bool = False) -> tuple[Path, Path]:
    """Stage and publish same-stem YAML and Markdown reports."""
    markdown_path, yaml_path = output_paths(output)
    if not overwrite:
        existing = [str(path) for path in (markdown_path, yaml_path) if path.exists()]
        if existing:
            raise FileExistsError(f"result file already exists: {', '.join(existing)}")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    data = result.model_dump(mode="json")
    yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)
    markdown_text = render_markdown(result)
    staged: list[tuple[Path, Path]] = []
    try:
        for destination, content in ((yaml_path, yaml_text), (markdown_path, markdown_text)):
            handle, temporary = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            temporary_path = Path(temporary)
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            staged.append((temporary_path, destination))
        for temporary_path, destination in staged:
            os.replace(temporary_path, destination)
    finally:
        for temporary_path, _ in staged:
            temporary_path.unlink(missing_ok=True)
    return markdown_path, yaml_path


def render_markdown(result: TestResult) -> str:
    """Render every canonical result field into a readable Markdown report."""
    lines = [result.title.rstrip(), "", "## Prompt", "", _fence(result.prompt)]
    lines.extend(["", "## Skills and marketplaces", ""])
    lines.extend(_bullets("Marketplaces", result.skills.marketplaces))
    lines.extend(_bullets("Plugins", result.skills.plugins))
    lines.extend(_bullets("Skill directories", result.skills.directories))

    run = result.run
    lines.extend(
        [
            "",
            "## Run summary",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Started | {_local_time(run.started_at)} |",
            f"| Finished | {_local_time(run.finished_at)} |",
            f"| Total duration | {_duration(run.total_duration_seconds)} |",
            f"| Codex execution | {_duration(run.codex_execution_seconds)} |",
            f"| Status | {run.status} |",
            f"| Exit code | {run.exit_code} |",
            f"| Distribution | {run.distro or '(default)'} |",
            f"| Codex version | {run.codex_version or '(unavailable)'} |",
            f"| Workspace | {run.workspace_path or '(not created)'} |",
            f"| Workspace retained | {run.workspace_retained} |",
        ]
    )
    if run.error:
        lines.extend(["", "**Error:**", "", _fence(run.error)])

    lines.extend(
        [
            "",
            "## Phase timing",
            "",
            "| Phase | Started | Finished | Seconds |",
            "| --- | --- | --- | ---: |",
        ]
    )
    for phase in result.timing.phases:
        lines.append(
            f"| {phase.name} | {_local_time(phase.started_at)} | "
            f"{_local_time(phase.finished_at)} | "
            f"{phase.duration_seconds:.2f} |"
        )

    lines.extend(["", "## Token usage", ""])
    if result.usage:
        lines.extend(
            [
                "| Model | Attribution | Input | Cached input | Output | Reasoning output |",
                "| --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for usage in result.usage:
            lines.append(
                f"| {usage.model} | {usage.attribution} | {_number(usage.input_tokens)} | "
                f"{_number(usage.cached_input_tokens)} | {_number(usage.output_tokens)} | "
                f"{_number(usage.reasoning_output_tokens)} |"
            )
    else:
        lines.append("No token usage event was reported.")

    lines.extend(["", "## Model pricing and calculated cost", ""])
    model_information = result.model_information
    lines.extend(
        [
            f"- Pricing file: `{model_information.pricing_file}`",
            f"- Currency: `{model_information.currency}`",
            "",
        ]
    )
    if model_information.models:
        lines.extend(
            [
                "| Model | Input rate / 1M | Cached rate / 1M | Output rate / 1M | "
                "Input cost | Cached cost | Output cost | Total cost |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for model in model_information.models:
            lines.append(
                f"| {model.model} | {_money(model.input_cost_per_million_tokens)} | "
                f"{_money(model.cached_input_cost_per_million_tokens)} | "
                f"{_money(model.output_cost_per_million_tokens)} | "
                f"{_money(model.input_cost)} | {_money(model.cached_input_cost)} | "
                f"{_money(model.output_cost)} | {_money(model.total_cost)} |"
            )
            if model.note:
                lines.extend(["", f"Pricing note for `{model.model}`: {model.note}"])
        lines.extend(["", f"**Aggregate cost:** {_money(model_information.total_cost)}"])
    else:
        lines.append("No model usage was reported, so no cost was calculated.")

    lines.extend(["", "## Final response", "", _fence(result.result.final_message or "")])
    lines.extend(["", "## Invocation", "", _fence(result.invocation, "text")])
    lines.extend(
        ["", "## Codex command", "", _fence(_display_command(result.command.argv), "text")]
    )
    lines.extend(_details("Resolved configuration", _yaml(result.configuration), "yaml"))

    inventory = "\n".join(
        f"{entry.type}\t{entry.size}\t{entry.path}"
        + (f" -> {entry.symlink_target}" if entry.symlink_target else "")
        for entry in result.workspace.files
    )
    lines.extend(_details("Workspace inventory", inventory, "text"))

    timing_values = [event.model_dump(mode="json") for event in result.timing.trace_events]
    timing_text = _yaml(_localize_value(timing_values))
    lines.extend(_details("Trace timing details", timing_text, "yaml"))
    lines.extend(
        _details("Complete Codex stdout JSONL", _localize_jsonl(result.logs.stdout_jsonl), "jsonl")
    )
    lines.extend(_details("Complete Codex stderr", result.logs.stderr, "text"))
    for trace in result.logs.session_traces:
        lines.extend(
            _details(
                f"Session trace: {trace.path}",
                _pretty_jsonl(trace.content, localize=True),
                "json",
            )
        )
    lines.extend(["", f"Schema version: `{result.schema_version}`", ""])
    return "\n".join(lines)


def _details(summary: str, content: str, language: str) -> list[str]:
    return [
        "",
        "<details>",
        f"<summary>{summary}</summary>",
        "",
        _fence(content, language),
        "",
        "</details>",
    ]


def _fence(content: str, language: str = "text") -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{content}\n{fence}"


def _bullets(label: str, values: list[str]) -> list[str]:
    if not values:
        return [f"- {label}: none"]
    return [f"- {label}:"] + [f"  - `{value}`" for value in values]


def _yaml(value: object) -> str:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=1000).rstrip()


def _display_argv(argv: list[str]) -> str:
    return " ".join(f'"{item}"' if any(char.isspace() for char in item) else item for item in argv)


def _display_command(argv: list[str]) -> str:
    command = _display_argv(argv)
    if any("TEST_WSL2_LLM_ARG_" in item for item in argv):
        explanation = (
            "# TEST_WSL2_LLM_ARG_* values are base64-encoded WSL transport arguments "
            "(such as paths and the model name), not credentials or API tokens."
        )
        return f"{explanation}\n{command}"
    return command


def _pretty_jsonl(content: str, *, localize: bool = False) -> str:
    """Render valid JSONL as an indented JSON array without losing any events."""
    lines = [line for line in content.splitlines() if line.strip()]
    try:
        values = [json.loads(line) for line in lines]
    except json.JSONDecodeError:
        return content
    return json.dumps(_localize_value(values) if localize else values, indent=2, ensure_ascii=False)


def _local_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().isoformat()
    except ValueError:
        return value


def _localize_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _localize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_localize_value(item) for item in value]
    if isinstance(value, str) and ("T" in value or value.endswith("Z")):
        return _local_time(value)
    return value


def _localize_jsonl(content: str) -> str:
    lines = []
    for line in content.splitlines():
        try:
            value = json.loads(line)
            localized = _localize_value(value)
            lines.append(line if localized == value else json.dumps(localized, ensure_ascii=False))
        except json.JSONDecodeError:
            lines.append(line)
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes}m {secs}s"


def _number(value: int) -> str:
    return f"{value:,}"


def _money(value: float | None) -> str:
    return "unavailable" if value is None else f"${value:.2f}"
