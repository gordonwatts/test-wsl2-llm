"""Configuration and rendering helpers for template-driven batch runs."""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from test_wsl2_llm.compatibility import load_result_yaml
from test_wsl2_llm.config import load_config_file, output_stem
from test_wsl2_llm.models import TemplateCell, TestConfig, ValidatorConfig
from test_wsl2_llm.provenance import effective_configuration_hash
from test_wsl2_llm.validation import validate_configuration

_FIELD = re.compile(r"{{\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*}}")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TEMPLATE_SCHEMA_NAME = "test-wsl2-llm-template.schema.json"
_PACKAGED_SCHEMA_NAME = "template.schema.json"
_SCHEMA_HEADER = re.compile(r"^# yaml-language-server:\s*\$schema=.*$")
_QUESTION_STRING_LIST_FIELDS = frozenset({"copy_files", "copy_back", "plugins"})


@dataclass(frozen=True)
class TemplateCellCheck:
    """Classification of an existing result pair for template resume."""

    state: Literal["missing", "incomplete", "succeeded", "failed", "stale"]
    reason: str = ""


def template_cell_fingerprint(config: TestConfig) -> str:
    """Hash effective settings using the same canonical identity as run provenance."""
    return effective_configuration_hash(config)

def template_cell_metadata(question_id: str, repetition: int, config: TestConfig) -> TemplateCell:
    """Build the persisted identity for one expanded template cell."""
    return TemplateCell(
        question_id=question_id,
        model_selector=config.model_selector,
        repetition=repetition,
        fingerprint=template_cell_fingerprint(config),
    )


def inspect_template_result(
    markdown_path: Path,
    yaml_path: Path,
    expected: TemplateCell,
) -> TemplateCellCheck:
    """Validate a saved pair and classify whether it can satisfy a cell."""
    if not markdown_path.is_file() or not yaml_path.is_file():
        if markdown_path.exists() or yaml_path.exists():
            return TemplateCellCheck("incomplete", "the Markdown/YAML pair is incomplete")
        return TemplateCellCheck("missing")
    try:
        result = load_result_yaml(yaml_path)
    except (OSError, ValueError, TypeError) as exc:
        return TemplateCellCheck("incomplete", f"canonical YAML is unreadable: {exc}")
    except Exception as exc:
        return TemplateCellCheck("incomplete", f"canonical YAML is invalid: {exc}")
    actual = result.template_cell
    if actual is None:
        return TemplateCellCheck("incomplete", "saved result has no template cell identity")
    if (
        actual.question_id != expected.question_id
        or actual.model_selector.casefold() != expected.model_selector.casefold()
        or actual.repetition != expected.repetition
    ):
        return TemplateCellCheck("stale", "saved result belongs to a different template cell")
    if actual.fingerprint != expected.fingerprint:
        return TemplateCellCheck("stale", "effective prompt or run settings changed")
    return TemplateCellCheck(result.run.status)


TEMPLATE_STARTER = """# yaml-language-server: $schema=./template.schema.json
# Template-driven WSL2 Codex batch configuration
schema_version: 1
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
    # plugins: [question-specific@marketplace, -shared@marketplace]
    # validators: [{name: require_string, arguments: {string: "expected text"}}]

model: MODEL:medium
marketplaces: []
plugins: []
mcp_servers: []
copy_files: []
copy_back:
  - plot_*.png
  - script.py
target: wsl
distro: null
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
    models: list[str] | None = Field(default=None, min_length=1)
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
        for key in ("prompt_template", "questions", "models", "repeat", "threads")
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
        shared_copy_files=values.get("copy_files", []),
        shared_copy_back=values.get("copy_back", []),
        shared_plugins=values.get("plugins", []),
        shared_validators=values.get("validators", []),
        base=path.parent,
    )
    return batch, values, path


def validate_questions(
    prompt_template: str,
    questions: list[dict[str, Any]],
    *,
    shared_copy_files: list[str] | None = None,
    shared_copy_back: list[str] | None = None,
    shared_plugins: list[str] | None = None,
    shared_validators: list[ValidatorConfig | dict[str, Any]] | None = None,
    base: Path | None = None,
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
            if key in _QUESTION_STRING_LIST_FIELDS:
                if not isinstance(value, list) or any(
                    not isinstance(item, str) or not item.strip() for item in value
                ):
                    raise ValueError(
                        f"question {identifier} field '{key}' must be a list of "
                        "non-empty strings"
                    )
                continue
            if key == "distro":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"question {identifier} field 'distro' must be a non-empty string"
                    )
                continue
            if key == "validators":
                if not isinstance(value, list):
                    raise ValueError(f"question {identifier} field 'validators' must be a list")
                try:
                    specifications = [ValidatorConfig.model_validate(item) for item in value]
                except Exception as exc:
                    raise ValueError(
                        f"question {identifier} field 'validators' contains an invalid check: {exc}"
                    ) from exc
                validate_configuration(specifications)
                continue
            if value is None or isinstance(value, (dict, list, tuple)):
                raise ValueError(f"question {identifier} field '{key}' must be a scalar value")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"question {identifier} field '{key}' must be a scalar value")
    question_ids = {str(question["id"]) for question in questions}
    prompt_tokens = {match.group(1) for match in _FIELD.finditer(prompt_template)}
    required_texts = {
        identifier
        for identifier, question in ((str(q["id"]), q) for q in questions)
        if isinstance(question.get("question"), str) and "question" in prompt_tokens
    }
    required_texts.update(prompt_tokens & question_ids)
    question_texts = _resolve_question_texts(questions, required=required_texts)
    for question in questions:
        identifier = str(question["id"])
        values = dict(question)
        if identifier in question_texts:
            values["question"] = question_texts[identifier]
        render_template(prompt_template, values, identifier, question_texts=question_texts)
        question_copy_files(list(shared_copy_files or []), question, base=base)
        question_copy_back(list(shared_copy_back or []), question)
        question_plugins(list(shared_plugins or []), question)
        effective_validators = list(shared_validators or [])
        if "validators" in question:
            effective_validators = question_validators(effective_validators, question)
        try:
            specifications = [
                item if isinstance(item, ValidatorConfig) else ValidatorConfig.model_validate(item)
                for item in effective_validators
            ]
            validate_configuration(specifications)
        except Exception as exc:
            raise ValueError(f"question {identifier} has invalid validators: {exc}") from exc


def render_template(
    template: str,
    values: dict[str, Any],
    identifier: str = "question",
    *,
    question_texts: dict[str, str] | None = None,
) -> str:
    """Render supported ``{{ field }}`` expressions with strict validation."""
    matches = list(_FIELD.finditer(template))
    masked = _FIELD.sub("", template)
    if "{{" in masked or "}}" in masked or "{%" in template or "%}" in template:
        raise ValueError(f"question {identifier} contains an unsupported template expression")
    references = question_texts or {}
    unsupported = sorted(
        {
            match.group(1)
            for match in matches
            if "." in match.group(1)
            and match.group(1) not in values
            and match.group(1) not in references
        }
    )
    if unsupported:
        raise ValueError(f"question {identifier} contains an unsupported template expression")
    missing = sorted(
        {
            match.group(1)
            for match in matches
            if match.group(1) not in values and match.group(1) not in references
        }
    )
    if missing:
        raise ValueError(
            f"question {identifier} is missing template field(s): {', '.join(missing)}"
        )

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in values:
            return references[token]
        value = values[token]
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _FIELD.sub(replace, template)


def _resolve_question_texts(
    questions: list[dict[str, Any]], *, required: set[str] | None = None
) -> dict[str, str]:
    """Resolve reusable question text, detecting missing references and cycles."""
    question_by_id = {str(question["id"]): question for question in questions}
    cache: dict[str, str] = {}
    resolving: list[str] = []

    def resolve(identifier: str) -> str:
        if identifier in cache:
            return cache[identifier]
        if identifier in resolving:
            cycle = " -> ".join([*resolving, identifier])
            raise ValueError(f"question {identifier} has a circular text reference: {cycle}")
        question = question_by_id[identifier]
        raw_text = question.get("question")
        if not isinstance(raw_text, str):
            raise ValueError(
                f"question {identifier} cannot be reused because it has no question text"
            )
        resolving.append(identifier)
        try:
            references: dict[str, str] = {}
            for match in _FIELD.finditer(raw_text):
                token = match.group(1)
                if token in question_by_id and token not in question:
                    references[token] = resolve(token)
            rendered = render_template(
                raw_text,
                question,
                identifier,
                question_texts=references,
            )
        finally:
            resolving.pop()
        cache[identifier] = rendered
        return rendered

    for identifier in required or set():
        resolve(identifier)
    return cache


def render_questions(batch: TemplateConfig) -> list[tuple[str, str, dict[str, Any]]]:
    """Return question id, rendered prompt, and source values in YAML order."""
    question_ids = {str(question["id"]) for question in batch.questions}
    prompt_tokens = {match.group(1) for match in _FIELD.finditer(batch.prompt_template)}
    required_texts = {
        identifier
        for identifier, question in ((str(q["id"]), q) for q in batch.questions)
        if isinstance(question.get("question"), str) and "question" in prompt_tokens
    }
    required_texts.update(prompt_tokens & question_ids)
    question_texts = _resolve_question_texts(batch.questions, required=required_texts)
    rendered: list[tuple[str, str, dict[str, Any]]] = []
    for question in batch.questions:
        identifier = str(question["id"])
        values = dict(question)
        if identifier in question_texts:
            values["question"] = question_texts[identifier]
        rendered.append(
            (
                identifier,
                render_template(
                    batch.prompt_template,
                    values,
                    identifier,
                    question_texts=question_texts,
                ),
                values,
            )
        )
    return rendered


def _question_string_list(
    shared: list[str],
    question: dict[str, Any],
    field: str,
    *,
    normalize: Callable[[str], str] = lambda item: item,
) -> list[str]:
    """Apply ordered question additions and removals to an inherited string list."""
    items = [normalize(item) for item in shared]
    overrides = question.get(field, [])
    if not isinstance(overrides, list):
        raise ValueError(f"question {question.get('id', 'unknown')} field '{field}' must be a list")
    for override in overrides:
        if not isinstance(override, str) or not override.strip():
            raise ValueError(
                f"question {question.get('id', 'unknown')} field '{field}' must be a list "
                "of non-empty strings"
            )
        if override.startswith("-"):
            if not override[1:].strip():
                raise ValueError(
                    f"question {question.get('id', 'unknown')} {field} removal "
                    f"'{override}' has no preceding item"
                )
            removed = normalize(override[1:])
            if removed not in items:
                raise ValueError(
                    f"question {question.get('id', 'unknown')} {field} removal "
                    f"'{override}' has no preceding item"
                )
            items = [item for item in items if item != removed]
        else:
            added = normalize(override)
            if added not in items:
                items.append(added)
    return items


def question_copy_back(shared: list[str], question: dict[str, Any]) -> list[str]:
    """Apply a question's copy-back additions and removals to shared patterns."""
    return _question_string_list(shared, question, "copy_back")


def question_copy_files(
    shared: list[str], question: dict[str, Any], *, base: Path | None = None
) -> list[str]:
    """Apply a question's copy-file additions and removals to shared sources.

    Relative question-level paths are resolved against ``base`` when supplied.
    This keeps template YAML paths independent of the process working directory.
    """
    def normalize(source: str) -> str:
        if base is None:
            return source
        path = Path(source).expanduser()
        return str((base / path).resolve()) if not path.is_absolute() else str(path.resolve())

    return _question_string_list(shared, question, "copy_files", normalize=normalize)


def question_distro(shared: str | None, question: dict[str, Any]) -> str | None:
    """Return a question distro override, or the shared distribution."""
    if "distro" not in question:
        return shared
    override = question["distro"]
    if not isinstance(override, str) or not override.strip():
        raise ValueError(
            f"question {question.get('id', 'unknown')} field 'distro' must be a non-empty string"
        )
    return override


def question_plugins(shared: list[str], question: dict[str, Any]) -> list[str]:
    """Apply a question's plugin additions and removals to shared plugins."""
    return _question_string_list(shared, question, "plugins")


def question_validators(
    shared: list[ValidatorConfig | dict[str, Any]], question: dict[str, Any]
) -> list[ValidatorConfig | dict[str, Any]]:
    """Return question validators, replacing shared checks when supplied."""
    if "validators" not in question:
        return list(shared)
    overrides = question["validators"]
    if not isinstance(overrides, list):
        raise ValueError(
            f"question {question.get('id', 'unknown')} field 'validators' must be a list"
        )
    return list(overrides)


def question_title(identifier: str, question: dict[str, Any], prompt: str) -> str:
    """Build a one-line report heading from the question text or rendered prompt."""
    text = " ".join(str(question.get("question", prompt)).split())
    return f"# Question: {identifier} - {text[:30]}..."


def _filename_component(value: str) -> str:
    """Return a portable report-name component with punctuation as hyphens."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")


def template_output(
    output: str,
    identifier: str,
    index: int,
    repeat: int,
    model_selector: str | None = None,
) -> str:
    """Build a result stem for one question/repetition."""
    path = output_stem(output)
    # Normalize every filename component to portable ASCII separators. In
    # particular, do not leave percent-encoded dots or colons in report names.
    path = path.with_name(_filename_component(path.name))
    path = path.with_name(f"{path.name}-{_filename_component(identifier)}")
    if model_selector is not None:
        path = path.with_name(f"{path.name}-{_filename_component(model_selector)}")
    if repeat > 1:
        width = max(3, len(str(repeat)))
        path = path.with_name(f"{path.name}-{index:0{width}d}")
    return str(path)


def write_template(path: Path) -> Path:
    """Write a starter template and its adjacent editor schema."""
    path = path.resolve()
    if path.exists():
        raise FileExistsError(f"template file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    schema_path = _schema_path(path)
    schema_path.write_text(_packaged_schema_text(), encoding="utf-8")
    path.write_text(_template_text(path, schema_path), encoding="utf-8")
    return path


def ensure_template_schema(path: Path, *, update_yaml: bool = True) -> Path:
    """Ensure the fixed-name schema and local YAML reference beside the path."""
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"template file does not exist: {path}")
    schema_path = _schema_path(path)
    schema_text = _packaged_schema_text()
    if not schema_path.exists() or schema_path.read_text(encoding="utf-8") != schema_text:
        schema_path.write_text(schema_text, encoding="utf-8")
    if update_yaml:
        current = path.read_text(encoding="utf-8")
        updated = _with_schema_header(current, schema_path)
        if updated != current:
            path.write_text(updated, encoding="utf-8")
    return schema_path


def write_template_config(path: Path, values: dict[str, Any]) -> Path:
    """Write a resolved template YAML with its adjacent local schema reference."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    schema_path = _schema_path(path)
    schema_text = _packaged_schema_text()
    if not schema_path.exists() or schema_path.read_text(encoding="utf-8") != schema_text:
        schema_path.write_text(schema_text, encoding="utf-8")

    content = yaml.safe_dump(values, sort_keys=False, allow_unicode=True)
    path.write_text(_with_schema_header(content, schema_path), encoding="utf-8")
    return path


def _schema_path(path: Path) -> Path:
    return path.with_name(TEMPLATE_SCHEMA_NAME)


def _template_text(path: Path, schema_path: Path) -> str:
    """Render the starter with a schema reference tied to its actual location."""
    output = r".\results\{output_stem}" if os.name == "nt" else "./results/{output_stem}"
    starter = TEMPLATE_STARTER.replace(r".\results\{output_stem}", output)
    return _with_schema_header(starter.replace("{output_stem}", path.stem), schema_path)


def _with_schema_header(content: str, schema_path: Path) -> str:
    """Set or prepend the YAML language-server header using the adjacent filename."""
    header = f"# yaml-language-server: $schema={schema_path.name}"
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines[:3]):
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        if _SCHEMA_HEADER.match(line.rstrip("\r\n")):
            lines[index] = header + newline
            return "".join(lines)
    return header + "\n" + content


def _packaged_schema_text() -> str:
    """Read the schema bundled with the installed package."""
    resource = resources.files("test_wsl2_llm").joinpath(_PACKAGED_SCHEMA_NAME)
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    # Editable source checkouts keep the canonical schema at repository root.
    source = Path(__file__).resolve().parents[2] / _PACKAGED_SCHEMA_NAME
    return source.read_text(encoding="utf-8")


def resolved_template_values(
    batch: TemplateConfig,
    shared: dict[str, Any],
    *,
    output: str | None = None,
    repeat: int | None = None,
    threads: int | None = None,
) -> dict[str, Any]:
    """Return the resolved batch YAML values suitable for ``--save-config``."""
    values = dict(shared)
    values["prompt_template"] = batch.prompt_template
    values["questions"] = batch.questions
    values["repeat"] = batch.repeat if repeat is None else repeat
    values["threads"] = batch.threads if threads is None else threads
    if batch.models is not None:
        values["models"] = batch.models
    if output is not None:
        values["output"] = output
    return values
