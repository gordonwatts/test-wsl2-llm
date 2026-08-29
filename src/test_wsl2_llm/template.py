"""Configuration and rendering helpers for template-driven batch runs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from test_wsl2_llm.config import load_config_file

_FIELD = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

TEMPLATE_STARTER = """# Template-driven WSL2 Codex batch configuration
prompt_template: |
  Please write a stand-alone Python file that uv can run and auto-install
  dependencies for. It must do the following:

  {{ question }}

  Save plots as plot_<n>.png. Put all Python code in script.py.
  You must successfully run it on one input file and produce a plot
  before declaring success.

questions:
  - id: example
    question: Replace this with the question to run.

model: MODEL:medium
marketplaces: []
plugins: []
copy_files: []
copy_back:
  - plot_*.png
  - script.py
max_copy_back_files: 100
timeout_seconds: 1800
output: .\\results\\{output_stem}
repeat: 1
threads: 1
"""


class TemplateConfig(BaseModel):
    """Batch-only fields removed before constructing each ``TestConfig``."""

    model_config = ConfigDict(extra="forbid")

    prompt_template: str
    questions: list[dict[str, Any]] = Field(min_length=1)
    repeat: int = 1
    threads: int = 1

    @field_validator("prompt_template")
    @classmethod
    def non_empty_template(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt_template must not be empty")
        return value

    @field_validator("repeat", "threads")
    @classmethod
    def positive_count(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("must be at least 1")
        return value


def load_template_file(path: Path) -> tuple[TemplateConfig, dict[str, Any], Path]:
    """Load a template YAML and return batch fields, shared run fields, and its path."""
    path = path.resolve()
    values = load_config_file(path)
    batch_values = {
        key: values.pop(key)
        for key in ("prompt_template", "questions", "repeat", "threads")
        if key in values
    }
    # ``run --save-config`` writes the resolved single-run prompt. Keep accepting
    # that field so users can copy the saved YAML into a template and then add
    # ``prompt_template`` and ``questions``; the generated template prompt wins.
    values.pop("prompt", None)
    values.pop("prompt_file", None)
    try:
        batch = TemplateConfig.model_validate(batch_values)
    except Exception as exc:
        raise ValueError(f"invalid template configuration '{path}': {exc}") from exc
    validate_questions(
        batch.prompt_template,
        batch.questions,
        shared_copy_back=values.get("copy_back", []),
    )
    return batch, values, path


def validate_questions(
    prompt_template: str,
    questions: list[dict[str, Any]],
    *,
    shared_copy_back: list[str] | None = None,
) -> None:
    """Validate question records and all template fields before execution."""
    seen: set[str] = set()
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            raise ValueError(f"question {index} must be a mapping")
        if "id" not in question:
            raise ValueError(f"question {index} requires an id")
        identifier = question["id"]
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ValueError(
                f"question {index} id must start with a letter or digit and contain only "
                "letters, digits, periods, underscores, or hyphens"
            )
        if identifier in seen:
            raise ValueError(f"duplicate question id: {identifier}")
        seen.add(identifier)
        for key, value in question.items():
            if not isinstance(key, str) or not _NAME.fullmatch(key):
                raise ValueError(f"question {identifier} field names must be identifiers")
            if key == "copy_back":
                if not isinstance(value, list) or any(
                    not isinstance(pattern, str) or not pattern.strip() for pattern in value
                ):
                    raise ValueError(
                        f"question {identifier} field 'copy_back' must be a list of "
                        "non-empty strings"
                    )
                continue
            if value is None or isinstance(value, (dict, list, tuple)):
                raise ValueError(f"question {identifier} field '{key}' must be a scalar value")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"question {identifier} field '{key}' must be a scalar value")
        render_template(prompt_template, question, identifier)
        question_copy_back(list(shared_copy_back or []), question)


def render_template(template: str, values: dict[str, Any], identifier: str = "question") -> str:
    """Render supported ``{{ field }}`` expressions with strict validation."""
    matches = list(_FIELD.finditer(template))
    masked = _FIELD.sub("", template)
    if "{{" in masked or "}}" in masked or "{%" in template or "%}" in template:
        raise ValueError(f"question {identifier} contains an unsupported template expression")
    missing = sorted({match.group(1) for match in matches if match.group(1) not in values})
    if missing:
        raise ValueError(
            f"question {identifier} is missing template field(s): {', '.join(missing)}"
        )

    def replace(match: re.Match[str]) -> str:
        value = values[match.group(1)]
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _FIELD.sub(replace, template)


def render_questions(batch: TemplateConfig) -> list[tuple[str, str, dict[str, Any]]]:
    """Return question id, rendered prompt, and source values in YAML order."""
    return [
        (
            str(question["id"]),
            render_template(batch.prompt_template, question, str(question["id"])),
            question,
        )
        for question in batch.questions
    ]


def question_copy_back(shared: list[str], question: dict[str, Any]) -> list[str]:
    """Apply a question's copy-back additions and removals to shared patterns."""
    patterns = list(shared)
    overrides = question.get("copy_back", [])
    if not isinstance(overrides, list):
        return patterns
    for pattern in overrides:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        if pattern.startswith("-") and len(pattern) > 1:
            remove = pattern[1:]
            if remove not in patterns:
                raise ValueError(
                    f"question {question.get('id', 'unknown')} copy_back removal "
                    f"'-{remove}' has no preceding pattern"
                )
            patterns = [existing for existing in patterns if existing != remove]
        elif pattern not in patterns:
            patterns.append(pattern)
    return patterns


def template_output(output: str, identifier: str, index: int, repeat: int) -> str:
    """Build a result stem for one question/repetition."""
    path = Path(output)
    if path.suffix.lower() in {".md", ".yaml", ".yml"}:
        path = path.with_suffix("")
    path = path.with_name(f"{path.name}-{identifier}")
    if repeat > 1:
        width = max(3, len(str(repeat)))
        path = path.with_name(f"{path.name}-{index:0{width}d}")
    return str(path)


def write_template(path: Path) -> Path:
    """Write the starter template, refusing to replace an existing file."""
    path = path.resolve()
    if path.exists():
        raise FileExistsError(f"template file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        TEMPLATE_STARTER.replace("{output_stem}", path.stem),
        encoding="utf-8",
    )
    return path


def resolved_template_values(
    batch: TemplateConfig, shared: dict[str, Any], *, output: str | None = None,
    repeat: int | None = None, threads: int | None = None,
) -> dict[str, Any]:
    """Return the resolved batch YAML values suitable for ``--save-config``."""
    values = dict(shared)
    values["prompt_template"] = batch.prompt_template
    values["questions"] = batch.questions
    values["repeat"] = batch.repeat if repeat is None else repeat
    values["threads"] = batch.threads if threads is None else threads
    if output is not None:
        values["output"] = output
    return values
