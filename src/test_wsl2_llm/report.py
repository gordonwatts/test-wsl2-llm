"""Serialize paired machine-readable and human-readable run reports."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

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
    markdown_text = render_markdown(result, report_path=markdown_path)
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


def write_markdown(
    result: TestResult,
    output: str | Path,
    *,
    overwrite: bool = False,
    include_details: bool = False,
) -> Path:
    """Render a result to one Markdown file without rewriting its YAML source."""
    destination = Path(output)
    if destination.suffix.lower() in {".yaml", ".yml", ".md"}:
        destination = destination.with_suffix(".md")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"result file already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_markdown(result, report_path=destination, include_details=include_details),
        encoding="utf-8",
        newline="",
    )
    return destination


def render_markdown(
    result: TestResult,
    *,
    report_path: Path | None = None,
    include_details: bool = False,
) -> str:
    """Render every canonical result field into a readable Markdown report."""
    lines = [
        result.title.rstrip(),
        "",
        "## Prompt" + (" (continuing retained workspace)" if result.continued_from else ""),
        "",
        _blockquote(result.prompt),
        "",
        "## Final response",
        "",
        _blockquote(result.result.final_message or ""),
    ]
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
            f"| Run date | {_local_date(run.started_at)} |",
            f"| Started | {_local_clock(run.started_at)} |",
            f"| Finished | {_local_clock(run.finished_at)} |",
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
            "| Phase | Started | Finished | Duration |",
            "| --- | --- | --- | ---: |",
        ]
    )
    for phase in result.timing.phases:
        lines.append(
            f"| {phase.name} | {_local_clock(phase.started_at)} | "
            f"{_local_clock(phase.finished_at)} | {_compact_duration(phase.duration_seconds)} |"
        )

    lines.extend(["", "## Token usage and cost", ""])
    model_information = result.model_information
    lines.extend(
        [
            f"- Pricing file: `{model_information.pricing_file}`",
            f"- Currency: `{model_information.currency}`",
            "- Token counts come from the `usage` object on each captured `turn.completed` "
            "event in Codex stdout JSONL; the configured model is used because those events "
            "do not report a model name.",
            "- `input_tokens` is the total input-token count, including the "
            "`cached_input_tokens` subset. Uncached input is calculated as "
            "`input_tokens - cached_input_tokens`; cached input was served from the prompt "
            "cache and is priced at its separate cached-input rate.",
            "- Output tokens are reported separately. Costs use the uncached-input rate for "
            "uncached input, the cached-input rate for cached input, and the output rate for "
            "output tokens.",
            "",
        ]
    )
    if result.usage:
        costs = {(model.model, model.attribution): model for model in model_information.models}
        lines.extend(
            [
                "| Model | Attribution | Input | Cached input | Output | Reasoning output | "
                "USD total |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for usage in result.usage:
            model = costs.get((usage.model, usage.attribution))
            lines.append(
                f"| {usage.model} | {usage.attribution} | {_number(usage.input_tokens)} | "
                f"{_number(usage.cached_input_tokens)} | {_number(usage.output_tokens)} | "
                f"{_number(usage.reasoning_output_tokens)} | "
                f"{_money(model.total_cost if model else None)} |"
            )
        lines.append(
            f"| **Aggregate** |  |  |  |  |  | **{_money(model_information.total_cost)}** |"
        )
    else:
        lines.append("No token usage event was reported, so no cost was calculated.")

    lines.extend(["", "## Invocation", "", _fence(result.invocation, "text")])
    lines.extend(
        ["", "## Codex command", "", _fence(_display_command(result.command.argv), "text")]
    )

    inventory = "\n".join(
        f"{entry.type}\t{entry.size}\t{entry.path}"
        + (f" -> {entry.symlink_target}" if entry.symlink_target else "")
        for entry in result.workspace.files
    )
    lines.extend(_details("Workspace inventory", inventory, "text"))
    lines.extend(_copied_back_section(result, report_path))
    lines.extend(_details("Complete Codex stderr", result.logs.stderr, "text"))

    if include_details:
        prior_conversation = result.conversation[:-1]
        if prior_conversation:
            lines.extend(_conversation_details(prior_conversation))
        lines.extend(_details("Resolved configuration", _yaml(result.configuration), "yaml"))

        timing_values = [event.model_dump(mode="json") for event in result.timing.trace_events]
        timing_text = _yaml(_localize_value(timing_values))
        lines.extend(_details("Trace timing details", timing_text, "yaml"))
        lines.extend(
            _details(
                "Complete Codex stdout JSONL", _localize_jsonl(result.logs.stdout_jsonl), "jsonl"
            )
        )
        for trace in result.logs.session_traces:
            lines.extend(
                _details(
                    f"Session trace: {trace.path}",
                    _pretty_jsonl(trace.content, localize=True),
                    "json",
                )
            )
    lines.extend(_activity_section(result))
    lines.extend(["", f"Schema version: `{result.schema_version}`", ""])
    return "\n".join(lines)


def _copied_back_section(result: TestResult, report_path: Path | None) -> list[str]:
    """Render copied-back artifacts with links and type-specific previews."""
    if not result.copied_back:
        return []
    lines = ["", "## Copied-back files", ""]
    for file in result.copied_back:
        link = _file_link(file.destination, report_path)
        name = Path(file.destination).name
        if file.type == "image":
            image_source = _png_data_uri(file.destination) or link
            lines.extend([f"[![{name}]({image_source})]({link})", ""])
        else:
            lines.extend([f"### [{name}]({link})", ""])
        lines.append(f"- Source: `{file.source}`")
        lines.append(f"- Size: {_number(file.size)} bytes")
        if file.type == "root":
            if file.error:
                lines.extend(["", "**ROOT inspection error:**", "", _fence(file.error)])
            elif file.root_contents:
                lines.extend(
                    [
                        "",
                        "| Object | Type | Events | Branches |",
                        "| --- | --- | ---: | --- |",
                    ]
                )
                for item in file.root_contents:
                    branches = ", ".join(item.get("branches", []))
                    events = item.get("events", "")
                    lines.append(
                        f"| `{item.get('path', '')}` | {item.get('type', '')} | {events} | "
                        f"{branches} |"
                    )
            else:
                lines.extend(["", "ROOT file contains no listed objects."])
        elif file.type == "text" and file.text_preview is not None:
            lines.extend(["", "First 10 lines:", "", _fence(file.text_preview)])
        elif file.type not in {"image", "file"}:
            lines.extend(["", f"Type: `{file.type}`"])
        lines.append("")
    return lines


def _file_link(destination: str, report_path: Path | None = None) -> str:
    """Return a portable, URI-encoded relative Markdown link for a copied artifact."""
    destination_path = Path(destination).resolve()
    if report_path is None:
        link = destination_path.name
    else:
        link = os.path.relpath(destination_path, Path(report_path).resolve().parent)
    return quote(link.replace(os.sep, "/"), safe="/:@-._~!$&'()*+,;=")


def _png_data_uri(destination: str) -> str | None:
    """Encode a copied PNG only while rendering Markdown, never in the YAML result."""
    path = Path(destination)
    if path.suffix.lower() != ".png":
        return None
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:image/png;base64,{encoded}"


def _activity_section(result: TestResult) -> list[str]:
    """Show concise progress updates without exposing the raw session trace."""
    updates: list[str] = []
    seen: set[str] = set()
    final_message = re.sub(r"\s+", " ", result.result.final_message or "").strip()
    for trace in result.logs.session_traces:
        for line in trace.content.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if event.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            if payload.get("type") != "agent_message":
                continue
            message = payload.get("message")
            if not isinstance(message, str):
                continue
            message = re.sub(r"\s+", " ", message).strip()
            if not message or message == final_message or _is_review_decision(message):
                continue
            if len(message) > 500:
                message = message[:497].rstrip() + "..."
            if message not in seen:
                seen.add(message)
                updates.append(message)

    lines = [
        "",
        "<details>",
        "<summary>Model activity</summary>",
        "",
        "_Progress updates emitted during the run; the raw session trace remains in YAML._",
        "",
    ]
    if updates:
        lines.extend(f"- {update}" for update in updates)
    else:
        lines.append("No readable progress updates were recorded.")
    lines.extend(["", "</details>"])
    return lines


def _is_review_decision(message: str) -> bool:
    """Exclude auto-reviewer JSON from the human-facing activity summary."""
    try:
        value = json.loads(message)
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict) and {"risk_level", "outcome"}.issubset(value)


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


def _conversation_details(conversation: list[object]) -> list[str]:
    """Render each prior prompt and response as readable Markdown blocks."""
    lines = ["", "<details>", "<summary>Conversation history</summary>", ""]
    for index, turn in enumerate(conversation, start=1):
        prompt = getattr(turn, "prompt", "")
        final_response = getattr(turn, "final_response", None) or "(no final response was recorded)"
        lines.extend(
            [
                f"### Prompt {index}",
                "",
                _fence(prompt),
                "",
                f"### Final response {index}",
                "",
                _fence(final_response),
                "",
            ]
        )
    lines.append("</details>")
    return lines


def _fence(content: str, language: str = "text") -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{content}\n{fence}"


def _blockquote(content: str) -> str:
    """Render prose as a Markdown blockquote so viewers can wrap it naturally."""
    lines = content.splitlines()
    if not lines:
        return ">"
    return "\n".join(f"> {line}" if line else ">" for line in lines)


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


def _local_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()


def _local_date(value: str) -> str:
    try:
        return _local_datetime(value).strftime("%Y-%m-%d")
    except ValueError:
        return value


def _local_clock(value: str) -> str:
    try:
        return _local_datetime(value).strftime("%H:%M:%S")
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


def _compact_duration(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _number(value: int) -> str:
    return f"{value:,}"


def _money(value: float | None) -> str:
    return "unavailable" if value is None else f"${value:.2f}"
