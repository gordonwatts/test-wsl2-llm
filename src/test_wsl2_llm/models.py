"""Validated configuration and result schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TestConfig(BaseModel):
    """All behavior-affecting settings for one WSL Codex test."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    model: str
    marketplaces: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    distro: str | None = None
    wsl_parent: str = "/tmp"
    output: str
    overwrite: bool = False
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    network: bool = True
    approval_policy: Literal["untrusted", "on-request", "never"] = "on-request"
    approvals_reviewer: Literal["auto_review", "user"] = "auto_review"
    auth_source: str = "~/.codex/auth.json"
    progress_lines: int = 8
    cleanup: bool = False

    @field_validator("prompt", "model")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("progress_lines")
    @classmethod
    def positive_progress_lines(cls, value: int) -> int:
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


class WorkspaceResult(BaseModel):
    files: list[WorkspaceFile] = Field(default_factory=list)


class CommandResult(BaseModel):
    argv: list[str] = Field(default_factory=list)


class LogsResult(BaseModel):
    stdout_jsonl: str = ""
    stderr: str = ""
    session_traces: list[SessionTrace] = Field(default_factory=list)


class TestResult(BaseModel):
    schema_version: Literal[1] = 1
    prompt: str
    skills: SkillsResult
    run: RunResult
    timing: TimingResult
    configuration: dict[str, Any]
    usage: list[UsageRecord]
    result: FinalResult
    workspace: WorkspaceResult
    command: CommandResult
    logs: LogsResult
