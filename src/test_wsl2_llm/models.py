"""Validated configuration and result schemas."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh"]
ExecutionTargetName = Literal["wsl", "linux", "macos", "local", "ssh"]


@dataclass(frozen=True)
class ModelSelector:
    """The canonical model and reasoning-effort selector used by the CLI and YAML."""

    model: str
    reasoning_effort: ReasoningEffort = "medium"

    @classmethod
    def parse(cls, value: str) -> "ModelSelector":
        """Parse a ``MODEL[:EFFORT]`` value into its runner components."""
        if ":" not in value:
            return cls(value)
        model, separator, effort = value.rpartition(":")
        if not separator or not model or not effort:
            raise ValueError("model must use MODEL[:EFFORT]")
        if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("reasoning_effort must be one of minimal, low, medium, high, or xhigh")
        return cls(model, effort)  # type: ignore[arg-type]

    @property
    def selector(self) -> str:
        """Return the canonical single-field YAML representation."""
        return f"{self.model}:{self.reasoning_effort}"


class EnvironmentPolicy(BaseModel):
    """Windows environment filtering applied before launching WSL."""

    model_config = ConfigDict(extra="forbid")

    unset: list[str] = Field(default_factory=list)
    path_remove: list[str] = Field(default_factory=list)

    @field_validator("unset", "path_remove")
    @classmethod
    def non_empty_entries(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("entries must not be empty")
        return values


class ValidatorConfig(BaseModel):
    """A named validator and its keyword arguments."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    passed: bool
    message: str


class TestConfig(BaseModel):
    """All behavior-affecting settings for one isolated Codex test."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    title: str = "# WSL2 Codex test result"
    agent: str = "codex"
    model: str
    reasoning_effort: ReasoningEffort = "medium"
    marketplaces: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    copy_files: list[str] = Field(default_factory=list)
    copy_back: list[str] = Field(default_factory=list)
    validators: list[ValidatorConfig] = Field(default_factory=list)
    environment: EnvironmentPolicy = Field(default_factory=EnvironmentPolicy)
    distro: str | None = None
    wsl_parent: str = "/tmp"
    output: str
    overwrite: bool = False
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    network: bool = True
    approval_policy: Literal["untrusted", "on-request", "never"] = "on-request"
    approvals_reviewer: Literal["auto_review", "user"] = "auto_review"
    target: ExecutionTargetName = "wsl"
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_port: int | None = None
    remote_workspace_parent: str = "/tmp"
    ssh_connect_timeout_seconds: float = 10.0
    auth_source: str | None = None
    pricing_file: str | None = None
    progress_lines: int = 5
    timeout_seconds: float | None = 1800.0
    max_copy_back_files: int = 100
    cleanup: bool = True

    @property
    def model_selector(self) -> str:
        """Canonical model and effort identity."""
        return ModelSelector(self.model, self.reasoning_effort).selector

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Serialize the single-run spec with one canonical model selector field."""
        values = super().model_dump(*args, **kwargs)
        values["model"] = self.model_selector
        values.pop("reasoning_effort", None)
        return values

    @model_validator(mode="before")
    @classmethod
    def split_model_and_effort(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("model"), str):
            return value
        selection = ModelSelector.parse(value["model"])
        normalized = dict(value)
        normalized["model"] = selection.model
        if ":" in value["model"] or "reasoning_effort" not in normalized:
            normalized["reasoning_effort"] = selection.reasoning_effort
        return normalized

    @field_validator("prompt", "model", "agent")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("mcp_servers")
    @classmethod
    def valid_mcp_names(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("MCP server names must not be empty")
        return list(dict.fromkeys(values))

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

    @field_validator("ssh_port")
    @classmethod
    def valid_ssh_port(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 65535:
            raise ValueError("ssh_port must be between 1 and 65535")
        return value

    @field_validator("ssh_connect_timeout_seconds")
    @classmethod
    def valid_ssh_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("ssh_connect_timeout_seconds must be greater than zero")
        return value

    @model_validator(mode="after")
    def validate_ssh_settings(self) -> "TestConfig":
        if self.target == "ssh" and not self.ssh_host:
            raise ValueError("ssh_host is required when target is ssh")
        if self.target != "ssh" and any(
            value is not None for value in (self.ssh_host, self.ssh_user, self.ssh_port)
        ):
            raise ValueError("SSH connection settings require target=ssh")
        return self

    @field_validator("max_copy_back_files")
    @classmethod
    def positive_max_copy_back_files(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be at least 1")
        return value


class ConfigurationSnapshot(BaseModel, Mapping[str, Any]):
    """Typed, persisted view of a resolved ``TestConfig``.

    Result files are an API. Known configuration fields are typed while
    extra fields are retained so a newer producer can be inspected by an
    older reader without silently discarding data.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = 1
    prompt: str | None = None
    title: str | None = None
    agent: str | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    marketplaces: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    copy_files: list[str] = Field(default_factory=list)
    copy_back: list[str] = Field(default_factory=list)
    validators: list[ValidatorConfig] = Field(default_factory=list)
    environment: EnvironmentPolicy = Field(default_factory=EnvironmentPolicy)
    distro: str | None = None
    wsl_parent: str | None = None
    output: str | None = None
    overwrite: bool | None = None
    sandbox: str | None = None
    network: bool | None = None
    approval_policy: str | None = None
    approvals_reviewer: str | None = None
    auth_source: str | None = None
    pricing_file: str | None = None
    progress_lines: int | None = None
    timeout_seconds: float | None = None
    max_copy_back_files: int | None = None
    cleanup: bool | None = None

    def __getitem__(self, key: str) -> Any:
        return self.model_dump(mode="json", exclude_none=True)[key]

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def __iter__(self) -> Iterator[str]:
        return iter(self.model_dump(mode="json", exclude_none=True))

    def __len__(self) -> int:
        return len(self.model_dump(mode="json", exclude_none=True))

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump(mode="json", exclude_none=True).get(key, default)

    def items(self):
        return self.model_dump(mode="json", exclude_none=True).items()

    def keys(self):
        return self.model_dump(mode="json", exclude_none=True).keys()

    def values(self):
        return self.model_dump(mode="json", exclude_none=True).values()

    def update(self, values: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        updates = dict(values or {})
        updates.update(kwargs)
        for key, value in updates.items():
            setattr(self, key, value)


class PhaseTiming(BaseModel):
    name: str
    started_at: str
    finished_at: str
    duration_seconds: float
    timed_out: bool = False


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


class InputProvenance(BaseModel):
    """A public, content-addressed input used by a run."""

    kind: str
    requested: str
    resolved: str | None = None
    content_hash: str | None = None


class MarketplaceProvenance(BaseModel):
    """Requested marketplace source and its resolved checkout identity."""

    requested: str
    resolved: str | None = None
    selector: str | None = None
    version: str = "unknown"
    content_hash: str | None = None


class PluginProvenance(BaseModel):
    """Requested plugin selector and resolved manifest version."""

    requested: str
    resolved: str | None = None
    version: str = "unknown"


class Provenance(BaseModel):
    """Non-secret identity of the harness, runtime, and effective inputs."""

    harness_version: str
    agent: str
    agent_version: str = "unknown"
    target: str
    target_version: str = "unknown"
    configuration_hash: str
    inputs: list[InputProvenance] = Field(default_factory=list)
    marketplaces: list[MarketplaceProvenance] = Field(default_factory=list)
    plugins: list[PluginProvenance] = Field(default_factory=list)
    identity: str

class RunResult(BaseModel):
    started_at: str
    finished_at: str
    total_duration_seconds: float
    codex_execution_seconds: float
    status: Literal["succeeded", "failed"]
    exit_code: int
    target: ExecutionTargetName = "wsl"
    distro: str | None
    workspace_path: str | None
    workspace_retained: bool
    codex_version: str | None
    agent: str = "codex"
    agent_version: str | None = None
    agent_execution_seconds: float | None = None
    error: str | None = None
    timed_out: bool = False


class TimingResult(BaseModel):
    phases: list[PhaseTiming] = Field(default_factory=list)
    trace_events: list[TraceEvent] = Field(default_factory=list)


class FinalResult(BaseModel):
    final_message: str | None = None
    timed_out: bool = False


class TemplateCell(BaseModel):
    """Identity and effective-settings fingerprint for a template batch cell."""

    question_id: str
    model_selector: str
    repetition: int
    fingerprint: str


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
    template_cell: TemplateCell | None = None
    invocation: str = ""
    continued_from: str | None = None
    skills: SkillsResult
    run: RunResult
    timing: TimingResult
    configuration: ConfigurationSnapshot
    provenance: Provenance | None = None
    usage: list[UsageRecord]
    model_information: ModelInformation
    result: FinalResult
    conversation: list[ConversationTurn] = Field(default_factory=list)
    workspace: WorkspaceResult
    copied_back: list[CopiedBackFile] = Field(default_factory=list)
    missing_copy_back: list[str] = Field(default_factory=list)
    command: CommandResult
    logs: LogsResult
    validation: list[ValidationResult] = Field(default_factory=list)
