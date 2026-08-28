"""Validated configuration and result schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh"]


class TestConfig(BaseModel):
    """All behavior-affecting settings for one WSL Codex test."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    title: str = "# WSL2 Codex test result"
    model: str
    reasoning_effort: ReasoningEffort = "medium"
    marketplaces: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    copy_files: list[str] = Field(default_factory=list)
    copy_back: list[str] = Field(default_factory=list)
    distro: str | None = None
    wsl_parent: str = "/tmp"
    output: str
    overwrite: bool = False
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    network: bool = True
    approval_policy: Literal["untrusted", "on-request", "never"] = "on-request"
    approvals_reviewer: Literal["auto_review", "user"] = "auto_review"
    auth_source: str = "~/.codex/auth.json"
    pricing_file: str | None = None
    progress_lines: int = 5
    timeout_seconds: float | None = 1800.0
    max_copy_back_files: int = 100
    cleanup: bool = False

    @model_validator(mode="before")
    @classmethod
    def split_model_and_effort(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("model"), str):
            return value
        model = value["model"]
        if ":" not in model:
            return value
        base_model, separator, effort = model.rpartition(":")
        if not separator or not base_model or not effort:
            raise ValueError("model must use MODEL[:EFFORT]")
        normalized = dict(value)
        normalized["model"] = base_model
        normalized["reasoning_effort"] = effort
        return normalized

    @field_validator("prompt", "model")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("progress_lines")
    @classmethod
    def positive_progress_lines(cls, value: int) -> int:
        if not 1 <= value <= 5:
            raise ValueError("must be between 1 and 5")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("max_copy_back_files")
    @classmethod
    def positive_max_copy_back_files(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be at least 1")
        return value


class PhaseTiming(BaseModel):
    name: str
    started_at: str
    finished_at: str
    duration_seconds: float


class TimingField(BaseModel):
    path: str
    value: Any
    normalized_seconds: float | None = None


class TraceEvent(BaseModel):
    source: str
    sequence: int
    stream: str | None = None
    event_type: str | None = None
    received_at: str | None = None
    elapsed_seconds: float | None = None
    timing_fields: list[TimingField] = Field(default_factory=list)


class UsageRecord(BaseModel):
    model: str
    attribution: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


class ModelCost(BaseModel):
    model: str
    attribution: str
    pricing_available: bool
    currency: str
    rate_unit: str = "per_million_tokens"
    input_cost_per_million_tokens: float | None = None
    cached_input_cost_per_million_tokens: float | None = None
    output_cost_per_million_tokens: float | None = None
    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    input_cost: float | None = None
    cached_input_cost: float | None = None
    output_cost: float | None = None
    total_cost: float | None = None
    source: str | None = None
    note: str | None = None


class ModelInformation(BaseModel):
    pricing_file: str
    currency: str
    models: list[ModelCost] = Field(default_factory=list)
    total_cost: float | None = None


class WorkspaceFile(BaseModel):
    type: str
    path: str
    size: int
    symlink_target: str | None = None


class SessionTrace(BaseModel):
    path: str
    content: str


class SkillsResult(BaseModel):
    marketplaces: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)


class RunResult(BaseModel):
    started_at: str
    finished_at: str
    total_duration_seconds: float
    codex_execution_seconds: float
    status: Literal["succeeded", "failed"]
    exit_code: int
    distro: str | None
    workspace_path: str | None
    workspace_retained: bool
    codex_version: str | None
    error: str | None = None


class TimingResult(BaseModel):
    phases: list[PhaseTiming] = Field(default_factory=list)
    trace_events: list[TraceEvent] = Field(default_factory=list)


class FinalResult(BaseModel):
    final_message: str | None = None


class ConversationTurn(BaseModel):
    """One prompt and the response produced while working in a workspace."""

    prompt: str
    final_response: str | None = None


class WorkspaceResult(BaseModel):
    files: list[WorkspaceFile] = Field(default_factory=list)


class CopiedBackFile(BaseModel):
    """A file copied from the WSL workspace into the Windows result directory."""

    source: str
    destination: str
    type: str
    size: int
    text_preview: str | None = None
    root_contents: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class CommandResult(BaseModel):
    argv: list[str] = Field(default_factory=list)


class LogsResult(BaseModel):
    stdout_jsonl: str = ""
    stderr: str = ""
    session_traces: list[SessionTrace] = Field(default_factory=list)


class TestResult(BaseModel):
    schema_version: Literal[2] = 2
    prompt: str
    title: str = "# WSL2 Codex test result"
    invocation: str = ""
    continued_from: str | None = None
    skills: SkillsResult
    run: RunResult
    timing: TimingResult
    configuration: dict[str, Any]
    usage: list[UsageRecord]
    model_information: ModelInformation
    result: FinalResult
    conversation: list[ConversationTurn] = Field(default_factory=list)
    workspace: WorkspaceResult
    copied_back: list[CopiedBackFile] = Field(default_factory=list)
    missing_copy_back: list[str] = Field(default_factory=list)
    command: CommandResult
    logs: LogsResult
