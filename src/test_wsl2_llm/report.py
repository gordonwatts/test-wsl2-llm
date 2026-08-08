"""Serialize paired machine-readable and human-readable run reports."""

from __future__ import annotations

import os
import re
import tempfile
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
    lines = ["# WSL2 Codex test result", "", "## Prompt", "", _fence(result.prompt)]
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
            f"| Started | {run.started_at} |",
            f"| Finished | {run.finished_at} |",
            f"| Total duration | {run.total_duration_seconds:.6f} seconds |",
            f"| Codex execution | {run.codex_execution_seconds:.6f} seconds |",
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
            f"| {phase.name} | {phase.started_at} | {phase.finished_at} | "
            f"{phase.duration_seconds:.6f} |"
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
                f"| {usage.model} | {usage.attribution} | {usage.input_tokens} | "
                f"{usage.cached_input_tokens} | {usage.output_tokens} | "
                f"{usage.reasoning_output_tokens} |"
            )
    else:
        lines.append("No token usage event was reported.")

    lines.extend(["", "## Final response", "", _fence(result.result.final_message or "")])
    lines.extend(["", "## Command", "", _fence(_display_argv(result.command.argv), "text")])
    lines.extend(_details("Resolved configuration", _yaml(result.configuration), "yaml"))

    inventory = "\n".join(
        f"{entry.type}\t{entry.size}\t{entry.path}"
        + (f" -> {entry.symlink_target}" if entry.symlink_target else "")
        for entry in result.workspace.files
    )
    lines.extend(_details("Workspace inventory", inventory, "text"))

    timing_text = _yaml([event.model_dump(mode="json") for event in result.timing.trace_events])
    lines.extend(_details("Trace timing details", timing_text, "yaml"))
    lines.extend(_details("Complete Codex stdout JSONL", result.logs.stdout_jsonl, "jsonl"))
    lines.extend(_details("Complete Codex stderr", result.logs.stderr, "text"))
    for trace in result.logs.session_traces:
        lines.extend(_details(f"Session trace: {trace.path}", trace.content, "jsonl"))
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
