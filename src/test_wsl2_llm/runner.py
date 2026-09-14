"""Orchestration of isolated Codex runs across supported execution targets."""

import base64
import fnmatch
import hashlib
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TextIO
from urllib.parse import urlparse

import tomli_w
import uproot
import yaml
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from test_wsl2_llm.agents import AgentAdapter, validate_agent_capabilities
from test_wsl2_llm.config import output_stem
from test_wsl2_llm.models import (
    CommandResult,
    ConversationTurn,
    CopiedBackFile,
    EnvironmentPolicy,
    FinalResult,
    LogsResult,
    ModelInformation,
    PhaseTiming,
    RunResult,
    SessionTrace,
    SkillsResult,
    TestConfig,
    TestResult,
    TimingResult,
    TraceEvent,
    WorkspaceFile,
    WorkspaceResult,
)
from test_wsl2_llm.pricing import load_and_calculate_costs
from test_wsl2_llm.target import ExecutionTarget
from test_wsl2_llm.traces import (
    final_message_from_events,
    parse_json_line,
    trace_event_from_json,
    usage_from_events,
)
from test_wsl2_llm.validation import apply_validators, validate_configuration

LOGGER = logging.getLogger(__name__)
TIMEOUT_ERROR_PREFIX = "[test-wsl2-llm] Codex timed out after "
CANCELLATION_GRACE_SECONDS = 5.0


class CancellationCoordinator:
    """Coordinate admission and interruption of one or more test runs."""

    def __init__(self, *, grace_seconds: float = CANCELLATION_GRACE_SECONDS) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[str, Callable[[], None]] = {}
        self._started: set[str] = set()
        self.grace_seconds = max(0.1, grace_seconds)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def claim(self, job_id: str) -> bool:
        """Claim a job if cancellation has not yet been requested."""
        with self._lock:
            if self._cancelled.is_set():
                return False
            self._started.add(job_id)
            return True

    def register_process(self, job_id: str, stop: Callable[[], None]) -> None:
        """Register an active process, stopping it immediately if already cancelled."""
        with self._lock:
            if self._cancelled.is_set():
                should_stop = True
            else:
                self._active[job_id] = stop
                should_stop = False
        if should_stop:
            stop()

    def unregister_process(self, job_id: str) -> None:
        with self._lock:
            self._active.pop(job_id, None)

    def cancel(self) -> None:
        """Signal cancellation and stop active processes within a grace period."""
        self._cancelled.set()
        with self._lock:
            callbacks = list(self._active.values())
        workers = [threading.Thread(target=callback, daemon=True) for callback in callbacks]
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + self.grace_seconds
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))

    def started_jobs(self) -> set[str]:
        with self._lock:
            return set(self._started)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sanitized_windows_environment(
    policy: EnvironmentPolicy,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy the Windows environment and apply a case-insensitive WSL launch policy."""
    environment = dict(os.environ if source is None else source)
    names_to_unset = {name.casefold() for name in policy.unset}
    environment = {
        name: value for name, value in environment.items() if name.casefold() not in names_to_unset
    }
    wslenv_name = next((name for name in environment if name.casefold() == "wslenv"), None)
    if wslenv_name is not None and names_to_unset:
        environment[wslenv_name] = ":".join(
            entry
            for entry in environment[wslenv_name].split(":")
            if entry.split("/", 1)[0].casefold() not in names_to_unset
        )
    path_name = next((name for name in environment if name.casefold() == "path"), None)
    removed_paths: list[str] = []
    if path_name is not None and policy.path_remove:
        patterns = [pattern.strip().strip('"').casefold() for pattern in policy.path_remove]

        def should_remove(entry: str) -> bool:
            candidate = entry.strip().strip('"').casefold()
            return any(
                fnmatch.fnmatchcase(candidate, pattern)
                if any(character in pattern for character in "*?[")
                else candidate.startswith(pattern)
                for pattern in patterns
            )

        kept_paths: list[str] = []
        for entry in environment[path_name].split(";"):
            if should_remove(entry):
                removed_paths.append(entry)
            else:
                kept_paths.append(entry)
        environment[path_name] = ";".join(kept_paths)
    for entry in removed_paths:
        LOGGER.info("Removed Windows PATH entry before WSL launch: %s", entry)
    if not removed_paths:
        LOGGER.debug("No Windows PATH entries were removed before WSL launch.")
    return environment


class WslClient:
    """Invoke one WSL distribution without shell-interpolating caller values."""

    def __init__(
        self,
        distro: str | None = None,
        environment_policy: EnvironmentPolicy | None = None,
        *,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.distro = distro
        self.environment = sanitized_windows_environment(
            environment_policy or EnvironmentPolicy(), source_environment
        )

    def command(self, arguments: list[str]) -> list[str]:
        command = ["wsl.exe"]
        if self.distro:
            command.extend(["-d", self.distro])
        return [*command, "--", *arguments]

    def run(
        self,
        arguments: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        command = self.command(arguments)
        LOGGER.info("WSL command: %s", _display_command(command))
        completed = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            env=self.environment,
        )
        if check and completed.returncode:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"WSL command failed ({completed.returncode}): {stderr}")
        return completed

    def bash(
        self,
        script: str,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return self.run(
            self.shell_command(script, *arguments),
            input_bytes=input_bytes,
            check=check,
        )

    def login_bash(
        self,
        script: str,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run through the user's login/interactive shell so nvm-installed Codex is visible."""
        return self.run(
            self.shell_command(script, *arguments, interactive_login=True),
            input_bytes=input_bytes,
            check=check,
        )

    def shell_command(
        self, script: str, *arguments: str, interactive_login: bool = False
    ) -> list[str]:
        """Encode values so wsl.exe cannot lose or reinterpret Bash positional arguments."""
        assignments: list[str] = []
        decoded: list[str] = []
        for index, argument in enumerate(arguments):
            name = f"TEST_WSL2_LLM_ARG_{index}"
            encoded = base64.b64encode(argument.encode("utf-8")).decode("ascii")
            assignments.append(f"{name}={encoded}")
            decoded.append(f'"$(printf %s "${name}" | base64 -d)"')
        prelude = f"set -- {' '.join(decoded)}; " if decoded else ""
        flags = "-lic" if interactive_login else "-lc"
        # wsl.exe expands unescaped dollar expressions while reconstructing the Linux
        # command line. One backslash is consumed at that boundary, leaving Bash the
        # intended variable and command-substitution syntax.
        wsl_safe_script = (prelude + script).replace("$", "\\$")
        return ["env", *assignments, "bash", flags, wsl_safe_script]

    def text(self, completed: subprocess.CompletedProcess[bytes]) -> str:
        return completed.stdout.decode("utf-8", errors="replace")

    def start_process(
        self,
        command: list[str],
        *,
        stdin: int = subprocess.PIPE,
        stdout: int = subprocess.PIPE,
        stderr: int = subprocess.PIPE,
    ) -> subprocess.Popen[str]:
        """Start a target process with the target's sanitized environment."""
        return subprocess.Popen(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self.environment,
        )

    def stop_process(self, process: subprocess.Popen[str]) -> None:
        """Stop a streamed process and its children on Windows."""
        if getattr(process, "poll", lambda: None)() is not None:
            return
        pid = getattr(process, "pid", None)
        if os.name == "nt" and isinstance(pid, int):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        else:
            terminate = getattr(process, "terminate", None)
            if not callable(terminate):
                return
            terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        except TypeError:
            process.wait()

    def to_target_path(self, host_path: str) -> str:
        """Convert a host path to an absolute path understood by WSL."""
        return self.text(self.bash('wslpath -a "$1"', host_path)).strip()

    def copy_to_target(
        self, host_path: str, target_directory: str, *, recursive: bool = False
    ) -> None:
        """Copy one host file or directory into a target directory."""
        if recursive:
            self.bash(
                'mkdir -p "$1" && cp -a -- "$2"/. "$1"/',
                target_directory,
                self.to_target_path(host_path),
            )
        else:
            self.bash(
                'cp -- "$2" "$1/"',
                target_directory,
                self.to_target_path(host_path),
            )

    def copy_from_target(self, target_path: str, host_path: str) -> None:
        """Copy one target file to a host path."""
        self.bash('cp -- "$1" "$2"', target_path, self.to_target_path(host_path))

    def create_workspace(self, parent: str) -> str:
        """Create and initialize a per-run target workspace root."""
        run_root = self.text(self.bash('mktemp -d -p "$1" test-wsl2-llm-XXXXXXXX', parent)).strip()
        self.bash(
            'mkdir -p "$1" "$2"',
            f"{run_root}/workspace",
            f"{run_root}/.harness/inputs",
        )
        return run_root

    def cleanup_workspace(self, run_root: str) -> None:
        """Remove a per-run workspace root from the target."""
        self.bash('rm -rf -- "$1"', run_root)


def sanitized_linux_environment(
    policy: EnvironmentPolicy,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy a Linux environment and apply the configured filtering policy."""
    environment = dict(os.environ if source is None else source)
    names_to_unset = {name.casefold() for name in policy.unset}
    environment = {
        name: value for name, value in environment.items() if name.casefold() not in names_to_unset
    }
    path_name = next((name for name in environment if name.casefold() == "path"), None)
    if path_name is not None and policy.path_remove:
        patterns = [pattern.strip().strip('"').casefold() for pattern in policy.path_remove]

        def should_remove(entry: str) -> bool:
            candidate = entry.strip().strip('"').casefold()
            return any(
                fnmatch.fnmatchcase(candidate, pattern)
                if any(character in pattern for character in "*?[")
                else candidate.startswith(pattern)
                for pattern in patterns
            )

        environment[path_name] = os.pathsep.join(
            entry for entry in environment[path_name].split(os.pathsep) if not should_remove(entry)
        )
    return environment


class LinuxClient:
    """Run an isolated Codex workspace directly on a native Linux host."""

    distro = None

    def __init__(
        self,
        environment_policy: EnvironmentPolicy | None = None,
        *,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.environment = sanitized_linux_environment(
            environment_policy or EnvironmentPolicy(), source_environment
        )

    def command(self, arguments: list[str]) -> list[str]:
        return list(arguments)

    def run(
        self,
        arguments: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        command = self.command(arguments)
        LOGGER.info("Linux command: %s", _display_command(command))
        completed = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            env=self.environment,
        )
        if check and completed.returncode:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Linux command failed ({completed.returncode}): {stderr}")
        return completed

    def bash(
        self,
        script: str,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return self.run(
            self.shell_command(script, *arguments), input_bytes=input_bytes, check=check
        )

    def login_bash(
        self,
        script: str,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return self.run(
            self.shell_command(script, *arguments, interactive_login=True),
            input_bytes=input_bytes,
            check=check,
        )

    def shell_command(
        self, script: str, *arguments: str, interactive_login: bool = False
    ) -> list[str]:
        """Build a Bash command using argv for positional values on native Linux."""
        flags = "-lic" if interactive_login else "-lc"
        return ["bash", flags, script, "test-wsl2-llm", *arguments]

    def text(self, completed: subprocess.CompletedProcess[bytes]) -> str:
        return completed.stdout.decode("utf-8", errors="replace")

    def start_process(
        self,
        command: list[str],
        *,
        stdin: int = subprocess.PIPE,
        stdout: int = subprocess.PIPE,
        stderr: int = subprocess.PIPE,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self.environment,
            start_new_session=True,
        )

    def stop_process(self, process: subprocess.Popen[str]) -> None:
        if getattr(process, "poll", lambda: None)() is not None:
            return
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                LOGGER.debug("process group already stopped")
        else:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if isinstance(pid, int):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    LOGGER.debug("process group did not exit after termination")
            else:
                process.kill()
        except TypeError:
            process.wait()

    def to_target_path(self, host_path: str) -> str:
        return str(Path(host_path).expanduser().resolve())

    def copy_to_target(
        self, host_path: str, target_directory: str, *, recursive: bool = False
    ) -> None:
        source = Path(host_path).expanduser().resolve()
        destination = Path(target_directory)
        destination.mkdir(parents=True, exist_ok=True)
        if recursive:
            if not source.is_dir():
                raise ValueError(f"copy source is not a directory: {source}")
            shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(source, destination / source.name)

    def copy_from_target(self, target_path: str, host_path: str) -> None:
        destination = Path(host_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(target_path), destination)

    def create_workspace(self, parent: str) -> str:
        parent_path = Path(parent).expanduser().resolve()
        parent_path.mkdir(parents=True, exist_ok=True)
        run_root = Path(tempfile.mkdtemp(prefix="test-wsl2-llm-", dir=parent_path))
        (run_root / "workspace").mkdir()
        (run_root / ".harness" / "inputs").mkdir(parents=True)
        return str(run_root)

    def cleanup_workspace(self, run_root: str) -> None:
        shutil.rmtree(run_root)

    # These helpers keep the local target independent of GNU find/coreutils.
    # The WSL target intentionally continues to use its existing shell helpers.
    def resolve_path(self, path: str, *, require_directory: bool = False) -> str:
        value = Path(path).expanduser()
        if require_directory and not value.is_dir():
            raise FileNotFoundError(f"directory does not exist: {path}")
        if not require_directory and not value.is_file():
            raise FileNotFoundError(f"file is not readable: {path}")
        return str(value.resolve())

    def write_file(self, path: str, content: bytes) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    def remove_file(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)

    def copy_file_to_target(self, host_path: str, target_path: str) -> None:
        destination = Path(target_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(host_path).expanduser().resolve(), destination)
        destination.chmod(0o600)

    def list_child_directories(self, root: str) -> list[str]:
        directory = Path(root)
        if not directory.is_dir():
            return []
        return sorted(
            str(entry) for entry in directory.iterdir() if entry.is_dir() and not entry.is_symlink()
        )

    def find_skill_directories(self, roots: list[str]) -> list[str]:
        found: list[str] = []
        for root in roots:
            directory = Path(root)
            if not directory.is_dir():
                continue
            for candidate in directory.rglob("SKILL.md"):
                if candidate.is_file():
                    found.append(str(candidate.parent))
        return sorted(set(found))

    def expand_copy_back_pattern(self, pattern: str, workspace: str) -> list[str]:
        search = Path(pattern) if Path(pattern).is_absolute() else Path(workspace) / pattern
        if any(character in str(search) for character in "*?["):
            matches = search.parent.glob(search.name) if search.parent != search else []
        else:
            matches = [search]
        return sorted(str(match) for match in matches if match.is_file())

    def inventory(self, workspace: str) -> list[WorkspaceFile]:
        root = Path(workspace)
        if not root.is_dir():
            return []
        files: list[WorkspaceFile] = []

        def visit(directory: Path) -> None:
            try:
                entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
            except OSError:
                return
            for entry in entries:
                relative = entry.relative_to(root).as_posix()
                try:
                    stat = entry.lstat()
                except OSError:
                    continue
                if entry.is_symlink():
                    entry_type = "symlink"
                    symlink_target = os.readlink(entry)
                elif entry.is_dir():
                    entry_type = "directory"
                    symlink_target = None
                else:
                    entry_type = "file"
                    symlink_target = None
                files.append(
                    WorkspaceFile(
                        type=entry_type,
                        path=relative,
                        size=stat.st_size,
                        symlink_target=symlink_target,
                    )
                )
                if entry_type == "directory":
                    visit(entry)

        visit(root)
        return sorted(files, key=lambda entry: entry.path)

    def session_traces(self, codex_home: str) -> tuple[list[SessionTrace], list[TraceEvent]]:
        sessions = Path(codex_home) / "sessions"
        if not sessions.is_dir():
            return [], []
        traces: list[SessionTrace] = []
        events: list[TraceEvent] = []
        for path in sorted(sessions.rglob("*.jsonl")):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            relative = path.relative_to(Path(codex_home)).as_posix()
            traces.append(SessionTrace(path=relative, content=content))
            for sequence, line in enumerate(content.splitlines(), start=1):
                parsed = parse_json_line(line)
                if parsed:
                    events.append(
                        trace_event_from_json(
                            parsed,
                            source=f"session_trace:{relative}",
                            sequence=sequence,
                        )
                    )
        return traces, events


def create_execution_target(
    distro: str | None = None,
    environment_policy: EnvironmentPolicy | None = None,
    *,
    execution_target: str = "wsl",
    source_environment: Mapping[str, str] | None = None,
) -> ExecutionTarget:
    """Select WSL or native Linux while keeping one runner lifecycle."""
    if execution_target in {"linux", "macos", "local"}:
        if distro:
            raise ValueError("distro is only valid with the wsl execution target")
        return LinuxClient(environment_policy, source_environment=source_environment)
    if execution_target != "wsl":
        raise ValueError(f"unknown execution target: {execution_target}")
    if source_environment is None:
        return WslClient(distro, environment_policy)
    return WslClient(distro, environment_policy, source_environment=source_environment)


# Descriptive names for callers that do not need the historical LinuxClient name.
LocalClient = LinuxClient
MacOSClient = LinuxClient

# Descriptive name for callers that do not need the historical WslClient name.
WslExecutionTarget = WslClient


class RunState:
    def __init__(self) -> None:
        self.started_at = utc_now()
        self.started_monotonic = time.perf_counter()
        self.phases: list[PhaseTiming] = []

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started_at = utc_now()
        started = time.perf_counter()
        try:
            yield
        finally:
            self.phases.append(
                PhaseTiming(
                    name=name,
                    started_at=started_at,
                    finished_at=utc_now(),
                    duration_seconds=time.perf_counter() - started,
                )
            )


def _append_failure(error: str | None, label: str, failure: BaseException) -> str:
    """Keep the primary failure while adding a secondary recovery diagnostic."""
    detail = str(failure) or failure.__class__.__name__
    return f"{error + '; ' if error else ''}{label} failed: {detail}"


def _collect_evidence(
    client: WslClient,
    *,
    workspace_path: str | None,
    run_root: str | None,
    codex_home: str | None,
    config: TestConfig,
    state: RunState,
    files: list[WorkspaceFile],
    copied_back: list[CopiedBackFile],
    missing_copy_back: list[str],
    session_traces: list[SessionTrace],
    trace_events: list[TraceEvent],
    error: str | None,
    exit_code: int,
) -> tuple[
    list[WorkspaceFile],
    list[CopiedBackFile],
    list[SessionTrace],
    list[TraceEvent],
    str | None,
    int,
]:
    """Collect every available evidence source without masking the primary error.

    This is deliberately shared by fresh runs and retained-workspace continuations.
    Each source is attempted independently so a broken inventory or copy-back does
    not prevent the remaining evidence from being included in a report.
    """
    if not workspace_path or not run_root:
        return files, copied_back, session_traces, trace_events, error, exit_code

    try:
        with state.phase("workspace_inventory"):
            files = _inventory(client, workspace_path)
    except (Exception, KeyboardInterrupt) as collection_error:
        error = _append_failure(error, "workspace_inventory", collection_error)
        exit_code = exit_code or 1

    try:
        with state.phase("copy_back"):
            copied_back = _copy_back_files(
                client,
                config.copy_back,
                workspace_path,
                config.output,
                max_files=config.max_copy_back_files,
                missing=missing_copy_back,
            )
    except (Exception, KeyboardInterrupt) as collection_error:
        error = _append_failure(error, "copy_back", collection_error)
        exit_code = exit_code or 1

    if codex_home:
        try:
            with state.phase("session_trace_collection"):
                session_traces, session_events = _session_traces(client, codex_home)
            trace_events.extend(session_events)
        except (Exception, KeyboardInterrupt) as collection_error:
            error = _append_failure(error, "session_trace_collection", collection_error)
            exit_code = exit_code or 1

    return files, copied_back, session_traces, trace_events, error, exit_code


def _remove_auth(
    client: WslClient,
    codex_home: str | None,
    *,
    state: RunState,
    error: str | None,
    exit_code: int,
) -> tuple[str | None, int]:
    """Best-effort auth removal shared by run and continuation finalization."""
    if not codex_home:
        return error, exit_code
    try:
        with state.phase("auth_cleanup"):
            remover = (
                getattr(client, "remove_file", None)
                if hasattr(type(client), "remove_file")
                else None
            )
            if callable(remover):
                remover(f"{codex_home}/auth.json")
            else:
                client.bash('rm -f -- "$1/auth.json"', codex_home, check=False)
    except (Exception, KeyboardInterrupt) as auth_error:
        error = _append_failure(error, "auth cleanup", auth_error)
        exit_code = exit_code or 1
    return error, exit_code


def run_test(
    config: TestConfig,
    *,
    verbosity: int = 0,
    console: Console | None = None,
    live_progress: bool = True,
    log_callback: Callable[[str], None] | None = None,
    invocation: list[str] | None = None,
    report_callback: Callable[[TestResult], None] | None = None,
    target: ExecutionTarget | None = None,
    cancellation: CancellationCoordinator | None = None,
    job_id: str = "single",
) -> TestResult:
    """Execute one test and always return a reportable result after validation."""
    validate_configuration(config.validators)
    adapter = validate_agent_capabilities(config)
    console = console or Console(stderr=True)
    client = (
        target
        if target is not None
        else create_execution_target(
            config.distro, config.environment, execution_target=config.target
        )
    )
    state = RunState()
    codex_version: str | None = None
    workspace_path: str | None = None
    run_root: str | None = None
    codex_home: str | None = None
    command_argv: list[str] = []
    stdout = ""
    stderr = ""
    session_traces: list[SessionTrace] = []
    trace_events: list[TraceEvent] = []
    parsed_events: list[dict[str, Any]] = []
    skill_directories: list[str] = []
    codex_seconds = 0.0
    exit_code = 1
    error: str | None = None
    timed_out = False
    retained = False
    files: list[WorkspaceFile] = []
    copied_back: list[CopiedBackFile] = []
    missing_copy_back: list[str] = []
    runtime_marketplaces: list[str] = []
    resolved_parent = config.wsl_parent
    resolved_auth = config.auth_source
    model_information = ModelInformation(
        pricing_file=config.pricing_file or "bundled:model-pricing.yaml",
        currency="USD",
    )
    pricing_valid = False

    try:
        codex_configuration = _codex_config(config) if adapter.name == "codex" else ""
        model_information = load_and_calculate_costs([], config.pricing_file)
        pricing_valid = True
        with state.phase("preflight"):
            codex_version = adapter.preflight(client).version
            resolved_parent = _resolve_wsl_path(client, config.wsl_parent, require_directory=True)
            if adapter.capabilities.requires_auth:
                resolved_auth = _resolve_wsl_path(client, config.auth_source)

        with state.phase("workspace_creation"):
            run_root = client.create_workspace(resolved_parent)
            workspace_path = f"{run_root}/workspace"
            codex_home = f"{run_root}/.harness/codex-home"

        with state.phase("input_transfer"):
            _write_wsl_file(client, f"{run_root}/.harness/inputs/prompt.md", config.prompt)
            resolved_yaml = yaml.safe_dump(
                config.model_dump(mode="json"), sort_keys=False, allow_unicode=True
            )
            _write_wsl_file(client, f"{run_root}/.harness/inputs/config.yaml", resolved_yaml)
            runtime_marketplaces = _transfer_marketplaces(client, config.marketplaces, run_root)
            _transfer_files(client, config.copy_files, workspace_path)

        with state.phase("codex_home_setup"):
            if adapter.capabilities.requires_auth:
                _copy_auth(client, resolved_auth, codex_home)
            if codex_configuration:
                _write_wsl_file(client, f"{codex_home}/config.toml", codex_configuration)

        with state.phase("plugin_installation"):
            installed_plugin_roots: list[str] = []
            for source in runtime_marketplaces:
                client.login_bash(
                    'env CODEX_HOME="$1" codex plugin marketplace add "$2" --json',
                    codex_home,
                    source,
                )
            for plugin in config.plugins:
                installed = client.login_bash(
                    'env CODEX_HOME="$1" codex plugin add "$2" --json',
                    codex_home,
                    plugin,
                )
                installed_plugin_roots.extend(_installed_paths_from_json(client.text(installed)))
            skill_directories.extend(_skill_directories(client, "", installed_plugin_roots))
            skill_directories = sorted(set(skill_directories))

        with state.phase("codex_execution"):
            if cancellation is not None and cancellation.cancelled:
                raise KeyboardInterrupt
            codex_started = time.perf_counter()
            command_argv = adapter.command(
                client,
                home=codex_home,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
                workspace=workspace_path,
            )
            LOGGER.info("Codex command: %s", _display_command(command_argv))
            (
                exit_code,
                stdout,
                stderr,
                trace_events,
                parsed_events,
            ) = _stream_codex(
                command_argv,
                config.prompt,
                target=client,
                adapter=adapter,
                environment=client.environment,
                progress_lines=config.progress_lines,
                timeout_seconds=config.timeout_seconds,
                verbosity=verbosity,
                console=console,
                live_progress=live_progress,
                log_callback=log_callback,
                cancellation=cancellation,
                job_id=job_id,
            )
            codex_seconds = time.perf_counter() - codex_started
            timed_out = _is_timeout(exit_code, stderr)
            if timed_out:
                error = _timeout_error(stderr)
            elif exit_code:
                error = f"Codex exited with status {exit_code}"

            if timed_out and state.phases and state.phases[-1].name == "codex_execution":
                state.phases[-1].timed_out = True

    except (Exception, KeyboardInterrupt) as exc:  # Preserve a report even on interruption.
        error = str(exc) or "Run interrupted by keyboard interrupt"
        LOGGER.exception("WSL Codex test failed")
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else (exit_code or 1)
    finally:
        files, copied_back, session_traces, trace_events, error, exit_code = _collect_evidence(
            client,
            workspace_path=workspace_path,
            run_root=run_root,
            codex_home=codex_home,
            config=config,
            state=state,
            files=files,
            copied_back=copied_back,
            missing_copy_back=missing_copy_back,
            session_traces=session_traces,
            trace_events=trace_events,
            error=error,
            exit_code=exit_code,
        )
        error, exit_code = _remove_auth(
            client, codex_home, state=state, error=error, exit_code=exit_code
        )
        # A run workspace is retained until the report has been persisted.
        retained = run_root is not None

    finished_at = utc_now()
    usage = usage_from_events(parsed_events, config.model)
    if pricing_valid:
        model_information = load_and_calculate_costs(usage, config.pricing_file)
    final_message = final_message_from_events(parsed_events)
    result = TestResult(
        prompt=config.prompt,
        title=config.title,
        invocation=_display_argv(invocation or []),
        skills=SkillsResult(
            marketplaces=config.marketplaces,
            plugins=config.plugins,
            directories=skill_directories,
        ),
        run=RunResult(
            started_at=state.started_at,
            finished_at=finished_at,
            total_duration_seconds=time.perf_counter() - state.started_monotonic,
            codex_execution_seconds=codex_seconds,
            status="succeeded" if exit_code == 0 and error is None else "failed",
            exit_code=exit_code,
            target=config.target,
            distro=config.distro,
            workspace_path=workspace_path,
            workspace_retained=retained,
            codex_version=codex_version if adapter.name == "codex" else None,
            agent=config.agent,
            agent_version=codex_version,
            agent_execution_seconds=codex_seconds,
            error=error,
            timed_out=timed_out,
        ),
        timing=TimingResult(phases=state.phases, trace_events=trace_events),
        configuration=config.model_dump(mode="json"),
        usage=usage,
        model_information=model_information,
        result=FinalResult(final_message=final_message, timed_out=timed_out),
        conversation=[ConversationTurn(prompt=config.prompt, final_response=final_message)],
        workspace=WorkspaceResult(files=files),
        copied_back=copied_back,
        missing_copy_back=missing_copy_back,
        command=CommandResult(argv=command_argv),
        logs=LogsResult(
            stdout_jsonl=stdout,
            stderr=stderr,
            session_traces=session_traces,
        ),
    )
    result = apply_validators(result, config.validators)

    # Persist collected evidence before deleting the only WSL copy. A report-write
    # failure leaves the workspace available for recovery.
    if report_callback is not None:
        report_callback(result)
    if run_root and config.cleanup:
        try:
            with state.phase("workspace_cleanup"):
                client.cleanup_workspace(run_root)
            result.run.workspace_path = None
            result.run.workspace_retained = False
        except (Exception, KeyboardInterrupt) as cleanup_error:
            previous_error = result.run.error
            result.run.error = (
                f"{previous_error + '; ' if previous_error else ''}"
                f"workspace cleanup failed: {cleanup_error}"
            )
            result.run.exit_code = result.run.exit_code or 1
            result.run.status = "failed"
        result.timing.phases = state.phases
        result.run.finished_at = utc_now()
        result.run.total_duration_seconds = time.perf_counter() - state.started_monotonic
    return result


def continue_test(
    previous: TestResult,
    config: TestConfig,
    prompt: str,
    *,
    verbosity: int = 0,
    console: Console | None = None,
    invocation: list[str] | None = None,
    target: ExecutionTarget | None = None,
) -> TestResult:
    """Run a fresh Codex conversation in a retained result workspace."""
    workspace_path = previous.run.workspace_path
    if not workspace_path:
        raise ValueError("the result does not contain a retained workspace path")
    if not previous.run.workspace_retained:
        raise ValueError("the result workspace was not retained; rerun without --cleanup")

    validate_configuration(config.validators)
    adapter = validate_agent_capabilities(config)
    console = console or Console(stderr=True)
    client = (
        target
        if target is not None
        else create_execution_target(
            config.distro or previous.run.distro, config.environment, execution_target=config.target
        )
    )
    run_root = workspace_path.rsplit("/", 1)[0]
    codex_home = f"{run_root}/.harness/codex-home"
    state = RunState()
    history = list(previous.conversation) or [
        ConversationTurn(prompt=previous.prompt, final_response=previous.result.final_message)
    ]
    effective_prompt = continuation_prompt(history, prompt)
    codex_version: str | None = None
    resolved_auth = config.auth_source
    command_argv: list[str] = []
    stdout = ""
    stderr = ""
    trace_events: list[TraceEvent] = []
    parsed_events: list[dict[str, Any]] = []
    session_traces: list[SessionTrace] = []
    runtime_marketplaces: list[str] = []
    skill_directories: list[str] = list(previous.skills.directories)
    codex_seconds = 0.0
    exit_code = 1
    error: str | None = None
    timed_out = False
    files: list[WorkspaceFile] = []
    copied_back: list[CopiedBackFile] = []
    missing_copy_back: list[str] = []
    model_information = ModelInformation(
        pricing_file=config.pricing_file or "bundled:model-pricing.yaml", currency="USD"
    )
    pricing_valid = False

    try:
        codex_configuration = _codex_config(config) if adapter.name == "codex" else ""
        model_information = load_and_calculate_costs([], config.pricing_file)
        pricing_valid = True
        with state.phase("preflight"):
            codex_version = adapter.preflight(client).version
            if adapter.capabilities.requires_auth:
                resolved_auth = _resolve_wsl_path(client, config.auth_source)

        with state.phase("input_transfer"):
            continuation_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            _write_wsl_file(
                client,
                f"{run_root}/.harness/inputs/continuations/{continuation_id}-prompt.md",
                prompt,
            )
            prior_marketplaces = set(previous.skills.marketplaces)
            new_marketplaces = [
                source for source in config.marketplaces if source not in prior_marketplaces
            ]
            runtime_marketplaces = _transfer_marketplaces(
                client, new_marketplaces, run_root, append=True
            )
            prior_copy_files = set(previous.configuration.get("copy_files", []))
            new_copy_files = [
                source for source in config.copy_files if source not in prior_copy_files
            ]
            _transfer_files(client, new_copy_files, workspace_path)

        with state.phase("codex_home_setup"):
            if adapter.capabilities.requires_auth:
                _copy_auth(client, resolved_auth, codex_home)
            if codex_configuration:
                _write_wsl_file(client, f"{codex_home}/config.toml", codex_configuration)

        with state.phase("plugin_installation"):
            installed_plugin_roots: list[str] = []
            for source in runtime_marketplaces:
                client.login_bash(
                    'env CODEX_HOME="$1" codex plugin marketplace add "$2" --json',
                    codex_home,
                    source,
                )
            prior_plugins = set(previous.skills.plugins)
            new_plugins = [plugin for plugin in config.plugins if plugin not in prior_plugins]
            for plugin in new_plugins:
                installed = client.login_bash(
                    'env CODEX_HOME="$1" codex plugin add "$2" --json',
                    codex_home,
                    plugin,
                )
                installed_plugin_roots.extend(_installed_paths_from_json(client.text(installed)))
            skill_directories.extend(_skill_directories(client, codex_home, installed_plugin_roots))

        with state.phase("codex_execution"):
            codex_started = time.perf_counter()
            command_argv = adapter.command(
                client,
                home=codex_home,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
                workspace=workspace_path,
            )
            LOGGER.info("Codex command: %s", _display_command(command_argv))
            (
                exit_code,
                stdout,
                stderr,
                trace_events,
                parsed_events,
            ) = _stream_codex(
                command_argv,
                effective_prompt,
                target=client,
                adapter=adapter,
                environment=client.environment,
                progress_lines=config.progress_lines,
                timeout_seconds=config.timeout_seconds,
                verbosity=verbosity,
                console=console,
            )
            codex_seconds = time.perf_counter() - codex_started
            timed_out = _is_timeout(exit_code, stderr)
            if timed_out:
                error = _timeout_error(stderr)
            elif exit_code:
                error = f"Codex exited with status {exit_code}"

            if timed_out and state.phases and state.phases[-1].name == "codex_execution":
                state.phases[-1].timed_out = True

    except (Exception, KeyboardInterrupt) as exc:  # A continuation should still produce a report.
        error = str(exc) or "Continuation interrupted by keyboard interrupt"
        LOGGER.exception("WSL Codex continuation failed")
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else (exit_code or 1)
    finally:
        files, copied_back, session_traces, trace_events, error, exit_code = _collect_evidence(
            client,
            workspace_path=workspace_path,
            run_root=run_root,
            codex_home=codex_home,
            config=config,
            state=state,
            files=files,
            copied_back=copied_back,
            missing_copy_back=missing_copy_back,
            session_traces=session_traces,
            trace_events=trace_events,
            error=error,
            exit_code=exit_code,
        )
        error, exit_code = _remove_auth(
            client, codex_home, state=state, error=error, exit_code=exit_code
        )

    finished_at = utc_now()
    usage = usage_from_events(parsed_events, config.model)
    if pricing_valid:
        model_information = load_and_calculate_costs(usage, config.pricing_file)
    final_message = final_message_from_events(parsed_events)
    all_marketplaces = _unique([*previous.skills.marketplaces, *config.marketplaces])
    all_plugins = _unique([*previous.skills.plugins, *config.plugins])
    conversation = [*history, ConversationTurn(prompt=prompt, final_response=final_message)]
    continuation_config = config.model_dump(mode="json")
    continuation_config["continuation_of"] = workspace_path
    result = TestResult(
        prompt=prompt,
        title=config.title,
        invocation=_display_argv(invocation or []),
        continued_from=workspace_path,
        skills=SkillsResult(
            marketplaces=all_marketplaces,
            plugins=all_plugins,
            directories=_unique(skill_directories),
        ),
        run=RunResult(
            started_at=state.started_at,
            finished_at=finished_at,
            total_duration_seconds=time.perf_counter() - state.started_monotonic,
            codex_execution_seconds=codex_seconds,
            status="succeeded" if exit_code == 0 and error is None else "failed",
            exit_code=exit_code,
            target=config.target,
            distro=config.distro or previous.run.distro,
            workspace_path=workspace_path,
            workspace_retained=True,
            codex_version=codex_version if adapter.name == "codex" else previous.run.codex_version,
            agent=config.agent,
            agent_version=codex_version or previous.run.agent_version or previous.run.codex_version,
            agent_execution_seconds=codex_seconds,
            error=error,
            timed_out=timed_out,
        ),
        timing=TimingResult(phases=state.phases, trace_events=trace_events),
        configuration=continuation_config,
        usage=usage,
        model_information=model_information,
        result=FinalResult(final_message=final_message, timed_out=timed_out),
        conversation=conversation,
        workspace=WorkspaceResult(files=files),
        copied_back=copied_back,
        missing_copy_back=missing_copy_back,
        command=CommandResult(argv=command_argv),
        logs=LogsResult(
            stdout_jsonl=stdout,
            stderr=stderr,
            session_traces=session_traces,
        ),
    )
    result = apply_validators(result, config.validators)
    return result


def continuation_prompt(history: list[ConversationTurn], prompt: str) -> str:
    """Prefix a new prompt with the self-contained prompt/response chain."""
    chain = [
        "This working directory was created with the following list of prompts and responses.",
        "Use this history as context for the new prompt below.",
        "",
    ]
    for index, turn in enumerate(history, start=1):
        chain.extend(
            [
                f"Prompt {index}:",
                turn.prompt,
                "",
                "Final Response:",
                turn.final_response or "(no final response was recorded)",
                "",
            ]
        )
    chain.extend(["New prompt:", prompt])
    return "\n".join(chain)


def _skill_directories(
    client: ExecutionTarget, codex_home: str, installed_plugin_roots: list[str]
) -> list[str]:
    roots = _unique([root for root in [codex_home, *installed_plugin_roots] if root])
    native_finder = (
        getattr(client, "find_skill_directories", None)
        if hasattr(type(client), "find_skill_directories")
        else None
    )
    if callable(native_finder):
        return native_finder(roots)
    found: list[str] = []
    for root in roots:
        result = client.bash(
            'if test -d "$1"; then find "$1" -type f -name SKILL.md -printf "%h\\n"; fi',
            root,
        )
        found.extend(line for line in client.text(result).splitlines() if line)
    return _unique(found)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _to_target_path(client: ExecutionTarget, host_path: str) -> str:
    """Convert a host path through the target, with legacy-client compatibility."""
    converter = getattr(client, "to_target_path", None)
    if callable(converter):
        return converter(host_path)
    return client.text(client.bash('wslpath -a "$1"', host_path)).strip()


def _copy_to_target(
    client: ExecutionTarget,
    host_path: str,
    target_directory: str,
    *,
    directory: bool = False,
) -> None:
    copier = getattr(client, "copy_to_target", None)
    if callable(copier):
        try:
            copier(host_path, target_directory, recursive=directory)
        except TypeError:
            # Preserve compatibility with pre-interface test doubles.
            if directory:
                mounted = _to_target_path(client, host_path)
                client.bash(
                    'mkdir -p "$1" && cp -a -- "$2"/. "$1"/',
                    target_directory,
                    mounted,
                )
            else:
                copier(host_path, target_directory)
        return
    mounted = _to_target_path(client, host_path)
    command = 'mkdir -p "$1" && cp -a -- "$2"/. "$1"/' if directory else 'cp -- "$2" "$1/"'
    client.bash(command, target_directory, mounted)


def _copy_from_target(client: ExecutionTarget, target_path: str, host_path: str) -> None:
    copier = getattr(client, "copy_from_target", None)
    if callable(copier):
        copier(target_path, host_path)
        return
    client.bash('cp -- "$1" "$2"', target_path, _to_target_path(client, host_path))


def _transfer_marketplaces(
    client: ExecutionTarget, sources: list[str], run_root: str, *, append: bool = False
) -> list[str]:
    runtime: list[str] = []
    marketplace_root = f"{run_root}/.harness/inputs/marketplaces"
    client.bash('mkdir -p "$1"', marketplace_root)
    start_index = 1
    if append:
        directory_lister = (
            getattr(client, "list_child_directories", None)
            if hasattr(type(client), "list_child_directories")
            else None
        )
        if callable(directory_lister):
            existing_names = [Path(path).name for path in directory_lister(marketplace_root)]
        else:
            existing = client.bash(
                'if test -d "$1"; then find "$1" -mindepth 1 -maxdepth 1 -type d '
                '-name "marketplace-*" -printf "%f\\n"; fi',
                marketplace_root,
            )
            existing_names = client.text(existing).splitlines()
        indices = [
            int(name.removeprefix("marketplace-"))
            for name in existing_names
            if name.removeprefix("marketplace-").isdigit()
        ]
        start_index = max(indices, default=0) + 1
    for index, source in enumerate(sources, start=start_index):
        windows_path = Path(source)
        if windows_path.exists():
            destination = f"{marketplace_root}/marketplace-{index:03d}"
            _copy_to_target(
                client,
                str(windows_path.resolve()),
                destination,
                directory=True,
            )
            runtime.append(destination)
        elif _is_git_marketplace_source(source):
            destination = f"{marketplace_root}/marketplace-{index:03d}"
            repository, branch = _parse_git_marketplace_source(source)
            if branch is None:
                client.login_bash('git clone --depth 1 -- "$1" "$2"', repository, destination)
            else:
                client.login_bash(
                    'git clone --depth 1 --branch "$2" -- "$1" "$3"',
                    repository,
                    branch,
                    destination,
                )
            runtime.append(destination)
        else:
            runtime.append(source)
    return runtime


def _transfer_files(client: ExecutionTarget, sources: list[str], workspace: str) -> None:
    """Copy Windows files into the root of the WSL workspace before execution."""
    for windows_path in _validate_copy_file_sources(sources):
        _copy_to_target(client, str(windows_path.resolve()), workspace)


def _validate_copy_file_sources(sources: list[str]) -> list[Path]:
    """Validate copy-in files before any transfer, rejecting root-name collisions."""
    paths: list[Path] = []
    by_basename: dict[str, dict[str, str]] = {}
    for source in sources:
        windows_path = Path(source)
        if not windows_path.is_file():
            raise FileNotFoundError(f"copy file does not exist: {source}")
        paths.append(windows_path)
        by_basename.setdefault(windows_path.name.casefold(), {})[
            str(windows_path.resolve()).casefold()
        ] = str(windows_path.resolve())

    collisions = {
        basename: sorted(values.values())
        for basename, values in by_basename.items()
        if len(values) > 1
    }
    if collisions:
        details = "; ".join(
            f"{basename}: {', '.join(sources_for_name)}"
            for basename, sources_for_name in sorted(collisions.items())
        )
        raise ValueError(
            "copy_files contains colliding basenames; files are copied to the workspace "
            "root, so rename one of the sources or remove the duplicate. Collisions: "
            f"{details}"
        )
    return paths


def _copy_back_files(
    client: ExecutionTarget,
    sources: list[str],
    workspace: str,
    output: str,
    *,
    max_files: int | None = 100,
    missing: list[str] | None = None,
) -> list[CopiedBackFile]:
    """Copy requested workspace files beside the result and collect safe previews."""
    if not sources:
        return []
    output_stub = _output_stub(output)
    output_stub.parent.mkdir(parents=True, exist_ok=True)
    copied: list[CopiedBackFile] = []
    matches_by_pattern: list[tuple[str, list[str]]] = []
    seen_sources: set[str] = set()
    for pattern in sources:
        matches = _expand_copy_back_pattern(client, pattern, workspace)
        if not matches:
            LOGGER.warning("copy-back pattern did not match any files: %s", pattern)
            if missing is not None:
                missing.append(pattern)
            continue
        unique_matches = [match for match in matches if match not in seen_sources]
        seen_sources.update(unique_matches)
        matches_by_pattern.append((pattern, unique_matches))

    all_matches = [match for _, matches in matches_by_pattern for match in matches]
    if max_files is not None and len(all_matches) > max_files:
        LOGGER.warning(
            "copy-back limit reached; matched %d distinct files, copying only the first %d",
            len(all_matches),
            max_files,
        )
        all_matches = all_matches[:max_files]

    basename_counts: dict[str, int] = {}
    for workspace_source in all_matches:
        filename = PurePosixPath(workspace_source).name
        if not filename or filename in {".", ".."}:
            raise ValueError(f"copy-back path must name a file: {workspace_source}")
        basename_counts[filename.casefold()] = basename_counts.get(filename.casefold(), 0) + 1

    destinations: set[str] = set()
    for workspace_source in all_matches:
        destination = _copy_back_destination(
            output_stub,
            workspace,
            workspace_source,
            colliding_basenames={
                basename for basename, count in basename_counts.items() if count > 1
            },
            used_destinations=destinations,
        )
        _copy_from_target(client, workspace_source, str(destination.resolve()))
        source_name = workspace_source.removeprefix(f"{workspace}/")
        copied.append(_describe_copied_back(source_name, destination))
    return copied


def _copy_back_destination(
    output_stub: Path,
    workspace: str,
    workspace_source: str,
    *,
    colliding_basenames: set[str],
    used_destinations: set[str],
) -> Path:
    """Choose a readable, deterministic destination for one copied-back source."""
    filename = PurePosixPath(workspace_source).name
    source_name = workspace_source.removeprefix(f"{workspace}/")
    if filename.casefold() in colliding_basenames:
        readable_source = re.sub(r"[^A-Za-z0-9._-]+", "_", source_name.replace("/", "__"))
        destination = output_stub.parent / f"{output_stub.name}.{readable_source}"
    else:
        destination = output_stub.parent / f"{output_stub.name}.{filename}"

    destination_key = str(destination).casefold()
    if destination_key in used_destinations:
        digest = hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:8]
        destination = destination.with_name(f"{destination.stem}~{digest}{destination.suffix}")
        destination_key = str(destination).casefold()
    used_destinations.add(destination_key)
    return destination


def _expand_copy_back_pattern(client: ExecutionTarget, pattern: str, workspace: str) -> list[str]:
    """Expand a workspace-relative glob and retain regular files only."""
    native_expander = (
        getattr(client, "expand_copy_back_pattern", None)
        if hasattr(type(client), "expand_copy_back_pattern")
        else None
    )
    if callable(native_expander):
        return native_expander(pattern, workspace)
    completed = client.bash(
        """
pattern="$1"
workspace="$2"
if [[ "$pattern" = /* ]]; then
  search="$pattern"
else
  search="$workspace/$pattern"
fi
while IFS= read -r match; do
  if test -f "$match"; then
    printf '%s\\0' "$match"
  fi
done < <(compgen -G "$search")
""".strip(),
        pattern,
        workspace,
    )
    return [
        match.decode("utf-8", errors="replace") for match in completed.stdout.split(b"\0") if match
    ]


def _output_stub(output: str) -> Path:
    return output_stem(output).resolve()


def _describe_copied_back(source: str, destination: Path) -> CopiedBackFile:
    suffix = destination.suffix.lower()
    size = destination.stat().st_size
    if suffix == ".root":
        try:
            return CopiedBackFile(
                source=source,
                destination=str(destination),
                type="root",
                size=size,
                root_contents=_root_contents(destination),
            )
        except Exception as exc:
            return CopiedBackFile(
                source=source,
                destination=str(destination),
                type="root",
                size=size,
                error=str(exc),
            )
    if suffix in {".md", ".markdown"}:
        file_type = "markdown"
    elif _is_image(destination):
        file_type = "image"
    elif _is_text_file(destination):
        file_type = "text"
    else:
        file_type = "file"
    preview = None
    if file_type in {"text", "markdown"}:
        preview = "\n".join(
            destination.read_text(encoding="utf-8", errors="replace").splitlines()[:10]
        )
    return CopiedBackFile(
        source=source,
        destination=str(destination),
        type=file_type,
        size=size,
        text_preview=preview,
    )


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in {
        ".apng",
        ".avif",
        ".bmp",
        ".gif",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }


def _is_text_file(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(path.name)
    if mime and mime.startswith("text/"):
        return True
    try:
        sample = path.read_bytes()[:8192]
        if b"\0" in sample:
            return False
        sample.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return True


def _root_contents(path: Path) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    with uproot.open(path) as root_file:
        for key, item in root_file.items(recursive=True):
            classname = str(getattr(item, "classname", type(item).__name__))
            entry: dict[str, Any] = {"path": str(key), "type": classname}
            if classname == "TTree" or hasattr(item, "num_entries"):
                entry["events"] = int(item.num_entries)
                branch_names = item.keys()
                entry["branches"] = [str(branch) for branch in branch_names]
            contents.append(entry)
    return contents


def _is_git_marketplace_source(source: str) -> bool:
    """Recognize URL and scp-style Git repository sources."""
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "ssh", "git"} and bool(parsed.netloc):
        return True
    return bool(re.fullmatch(r"[^@\s]+@[^:\s]+:.+", source))


def _parse_git_marketplace_source(source: str) -> tuple[str, str | None]:
    """Split an optional trailing ``@branch`` selector from a Git source."""
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "ssh", "git"} and parsed.netloc:
        repository_path, separator, branch = parsed.path.rpartition("@")
        if separator and repository_path and branch:
            return parsed._replace(path=repository_path).geturl(), branch
        return source, None

    if _is_git_marketplace_source(source):
        repository, separator, branch = source.rpartition("@")
        if separator and branch and source.find(":") < source.rfind("@"):
            return repository, branch
    return source, None


def _installed_paths_from_json(output: str) -> list[str]:
    """Return installedPath values from a Codex CLI JSON response."""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        LOGGER.warning("Could not parse plugin installation JSON: %s", output.strip())
        return []

    paths: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "installedPath" and isinstance(child, str):
                    paths.append(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return paths


def _codex_config(config: TestConfig) -> str:
    content = "\n".join(
        [
            f"model = {json.dumps(config.model)}",
            f"model_reasoning_effort = {json.dumps(config.reasoning_effort)}",
            f"approval_policy = {json.dumps(config.approval_policy)}",
            f"approvals_reviewer = {json.dumps(config.approvals_reviewer)}",
            f"sandbox_mode = {json.dumps(config.sandbox)}",
            "",
            "[sandbox_workspace_write]",
            f"network_access = {str(config.network).lower()}",
            "",
        ]
    )

    servers = _load_mcp_servers(config.mcp_servers)
    if servers:
        content += "\n" + tomli_w.dumps({"mcp_servers": servers})
    return content


def _load_mcp_servers(names: list[str]) -> dict[str, Any]:
    """Import selected Windows Codex server tables without recording their contents."""
    if not names:
        return {}
    local_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    source = local_home / "config.toml"
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, ValueError):
        # TOML errors can contain source fragments, including credentials.
        raise ValueError(
            f"Cannot read local Codex MCP configuration '{source}' "
            "as TOML; check that the file exists and is valid."
        ) from None
    servers = document.get("mcp_servers", {})
    selected: dict[str, Any] = {}
    for name in names:
        if not isinstance(servers, dict) or name not in servers:
            raise ValueError(
                f"MCP server '{name}' was not found in '{source}' under [mcp_servers]."
            )
        if not isinstance(servers[name], dict):
            raise ValueError(f"MCP server '{name}' in '{source}' must be a TOML table.")
        selected[name] = servers[name]
    return selected


def _copy_auth(client: ExecutionTarget, source: str, codex_home: str) -> None:
    copier = (
        getattr(client, "copy_file_to_target", None)
        if hasattr(type(client), "copy_file_to_target")
        else None
    )
    if callable(copier):
        copier(source, f"{codex_home}/auth.json")
    else:
        client.bash(
            'mkdir -p "$1" && cp -- "$2" "$1/auth.json" && chmod 600 "$1/auth.json"',
            codex_home,
            source,
        )


def _write_wsl_file(client: ExecutionTarget, path: str, content: str) -> None:
    writer = getattr(client, "write_file", None) if hasattr(type(client), "write_file") else None
    if callable(writer):
        writer(path, content.encode())
    else:
        client.bash('mkdir -p "$(dirname "$1")" && cat > "$1"', path, input_bytes=content.encode())


def _resolve_wsl_path(
    client: ExecutionTarget, path: str, *, require_directory: bool = False
) -> str:
    resolver = (
        getattr(client, "resolve_path", None) if hasattr(type(client), "resolve_path") else None
    )
    if callable(resolver):
        return resolver(path, require_directory=require_directory)
    test = "test -d" if require_directory else "test -r"
    script = (
        'value="$1"; case "$value" in "~/"*) value="$HOME/${value:2}";; esac; '
        f'{test} "$value" && realpath -e "$value"'
    )
    return client.text(client.bash(script, path)).strip()


def _stream_codex(
    command: list[str],
    prompt: str,
    *,
    target: ExecutionTarget | None = None,
    adapter: AgentAdapter | None = None,
    environment: Mapping[str, str] | None = None,
    progress_lines: int,
    timeout_seconds: float | None = 1800.0,
    verbosity: int,
    console: Console,
    live_progress: bool = True,
    log_callback: Callable[[str], None] | None = None,
    cancellation: CancellationCoordinator | None = None,
    job_id: str = "single",
) -> tuple[int, str, str, list[TraceEvent], list[dict[str, Any]]]:
    if target is not None:
        process = target.start_process(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stop_lock = threading.Lock()
    process_registered = False
    process.stdin.write(prompt)
    process.stdin.close()
    messages: queue.Queue[tuple[str, str, str, float] | tuple[str, None, None, None]] = (
        queue.Queue()
    )
    started = time.perf_counter()
    deadline = started + timeout_seconds if timeout_seconds is not None else None
    interrupted = False
    timed_out = False

    def reader(name: str, stream: TextIO) -> None:
        for line in stream:
            messages.put((name, line, utc_now(), time.perf_counter() - started))
        messages.put((name, None, None, None))

    threads = [
        threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    raw = {"stdout": [], "stderr": []}
    sequences = {"stdout": 0, "stderr": 0}
    trace_events: list[TraceEvent] = []
    parsed_events: list[dict[str, Any]] = []
    recent: list[str] = []
    latest_meaningful: str | None = None
    completed_streams = 0
    stopped_at: float | None = None

    def stop_process() -> None:
        nonlocal stopped_at
        with stop_lock:
            if stopped_at is None:
                stopped_at = time.perf_counter()
            if target is not None:
                target.stop_process(process)
                return
            if getattr(process, "poll", lambda: None)() is not None:
                return
            terminate = getattr(process, "terminate", None)
            if not callable(terminate):
                return
            terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            except TypeError:
                process.wait()

    if cancellation is not None:
        cancellation.register_process(job_id, stop_process)
        process_registered = True

    def consume(live: Live | None) -> None:
        nonlocal completed_streams, interrupted, timed_out, latest_meaningful
        while completed_streams < 2:
            try:
                if (
                    deadline is not None
                    and getattr(process, "poll", lambda: None)() is None
                    and time.perf_counter() >= deadline
                ):
                    timed_out = True
                    stop_process()
                stream, line, received_at, elapsed = messages.get(timeout=0.25)
            except queue.Empty:
                if cancellation is not None and cancellation.cancelled:
                    interrupted = True
                    stop_process()
                if stopped_at is not None and time.perf_counter() - stopped_at > 5:
                    break
                continue
            except KeyboardInterrupt:
                interrupted = True
                stop_process()
                continue
            if line is None:
                completed_streams += 1
                continue
            sequences[stream] += 1
            raw[stream].append(line)
            parsed = parse_json_line(line)
            if parsed:
                normalized = adapter.normalize_event(parsed) if adapter is not None else parsed
                parsed_events.append(normalized)
                trace_events.append(
                    trace_event_from_json(
                        normalized,
                        source="stdout_jsonl" if stream == "stdout" else "stderr",
                        sequence=sequences[stream],
                        stream=stream,
                        received_at=received_at,
                        elapsed_seconds=elapsed,
                    )
                )
            else:
                trace_events.append(
                    TraceEvent(
                        source="stdout_jsonl" if stream == "stdout" else "stderr",
                        sequence=sequences[stream],
                        stream=stream,
                        event_type=None,
                        received_at=received_at,
                        elapsed_seconds=elapsed,
                    )
                )
            description = _progress_description(parsed, line)
            display = f"{_console_time(received_at)} [{stream}] {description}"
            if not _is_uninformative_progress(description):
                latest_meaningful = display
            recent.append(display)
            del recent[:-progress_lines]
            if verbosity >= 2:
                LOGGER.debug("[%s] %s", stream, line.rstrip("\r\n"))
            elif live:
                live.update(_progress_panel(recent, latest_meaningful))
            elif not live_progress:
                if log_callback is not None:
                    log_callback(display)
                else:
                    console.print(display)

    if verbosity >= 2 or not live_progress:
        consume(None)
    else:
        with Live(
            Panel("Starting Codex...", title="Codex progress"),
            console=console,
            refresh_per_second=8,
        ) as live:
            consume(live)
    for thread in threads:
        thread.join(timeout=5)
    if cancellation is not None and cancellation.cancelled:
        interrupted = True
        stop_process()
    try:
        exit_code = process.wait(timeout=5)
    except TypeError:
        exit_code = process.wait()
    except subprocess.TimeoutExpired:
        stop_process()
        try:
            exit_code = process.wait(timeout=5)
        except TypeError:
            exit_code = process.wait()
        except subprocess.TimeoutExpired:
            exit_code = 130 if interrupted else 124
    except KeyboardInterrupt:
        interrupted = True
        stop_process()
        exit_code = 130
    if timed_out:
        exit_code = 124
        raw["stderr"].append(f"{TIMEOUT_ERROR_PREFIX}{timeout_seconds:g} seconds.\n")
    elif interrupted:
        exit_code = 130
        raw["stderr"].append("[test-wsl2-llm] Codex run interrupted by keyboard interrupt.\n")
    if cancellation is not None and process_registered:
        cancellation.unregister_process(job_id)
    return (
        exit_code,
        "".join(raw["stdout"]),
        "".join(raw["stderr"]),
        trace_events,
        parsed_events,
    )


def _is_timeout(exit_code: int, stderr: str) -> bool:
    """Recognize a timeout without treating an unrelated exit 124 as one."""
    return exit_code == 124 and TIMEOUT_ERROR_PREFIX in stderr


def _timeout_error(stderr: str) -> str:
    """Return the concise timeout error while retaining the configured duration."""
    for line in stderr.splitlines():
        if line.startswith(TIMEOUT_ERROR_PREFIX):
            return line.removeprefix("[test-wsl2-llm] ").rstrip(".")
    return "Codex execution timed out"


def _progress_description(parsed: dict[str, Any] | None, raw_line: str) -> str:
    """Turn JSONL progress events into short, useful one-line status messages."""
    if not parsed:
        description = raw_line.rstrip()
    else:
        event_type = parsed.get("type")
        item = parsed.get("item")
        if event_type in {"item.started", "item.completed"} and isinstance(item, dict):
            phase = "Started" if event_type.endswith("started") else "Completed"
            item_type = str(item.get("type", "item")).replace("_", " ")
            if item_type == "command execution":
                command = re.sub(r"\s+", " ", str(item.get("command", ""))).strip()
                suffix = f": {command}" if command else ""
                if phase == "Completed" and item.get("exit_code") is not None:
                    suffix = f" (exit {item['exit_code']}){suffix}"
                description = f"{phase} command{suffix}"
            elif item_type == "agent message":
                text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
                description = f"{phase} model message: {text}" if text else f"{phase} model message"
            elif item_type == "file change":
                description = f"{phase} file changes"
            elif item_type == "web search":
                description = f"{phase} web search"
            else:
                description = f"{phase} {item_type}"
        elif isinstance(event_type, str):
            description = event_type.replace(".", " ").replace("_", " ").capitalize()
            if event_type.startswith("item."):
                description = f"{description} ({event_type})"
        else:
            description = raw_line.rstrip()
    description = re.sub(r"\s+", " ", description).strip()
    return description if len(description) <= 120 else description[:117].rstrip() + "..."


def _is_uninformative_progress(description: str) -> bool:
    """Identify routine MCP polling entries that should not replace the summary."""
    return description.casefold() in {"started mcp tool call", "completed mcp tool call"}


def _progress_panel(recent: list[str], latest_meaningful: str | None) -> Panel:
    """Render a bounded event log with a persistent summary of useful activity."""
    latest = latest_meaningful or "No meaningful activity yet."
    detail = "\n".join(recent) or "Starting Codex..."
    return Panel(
        f"Latest meaningful activity: {latest}\n\n{detail}",
        title="Codex progress",
    )


def _inventory(client: ExecutionTarget, workspace: str) -> list[WorkspaceFile]:
    native_inventory = (
        getattr(client, "inventory", None) if hasattr(type(client), "inventory") else None
    )
    if callable(native_inventory):
        return native_inventory(workspace)
    completed = client.bash(
        'if test -d "$1"; then find "$1" -mindepth 1 -printf \'%y\\0%s\\0%P\\0%l\\0\'; fi',
        workspace,
    )
    parts = completed.stdout.split(b"\0")
    if parts and not parts[-1]:
        parts.pop()
    files: list[WorkspaceFile] = []
    for index in range(0, len(parts), 4):
        if index + 3 >= len(parts):
            break
        kind, size, path, target = (
            part.decode("utf-8", errors="replace") for part in parts[index : index + 4]
        )
        files.append(
            WorkspaceFile(
                type={"f": "file", "d": "directory", "l": "symlink"}.get(kind, kind),
                path=path,
                size=int(size or 0),
                symlink_target=target or None,
            )
        )
    return sorted(files, key=lambda entry: entry.path)


def _session_traces(
    client: ExecutionTarget, codex_home: str | None
) -> tuple[list[SessionTrace], list[TraceEvent]]:
    if not codex_home:
        return [], []
    native_traces = (
        getattr(client, "session_traces", None) if hasattr(type(client), "session_traces") else None
    )
    if callable(native_traces):
        return native_traces(codex_home)
    listed = client.bash(
        'if test -d "$1/sessions"; then find "$1/sessions" -type f -name "*.jsonl" -print; fi',
        codex_home,
    )
    paths = [line for line in client.text(listed).splitlines() if line]
    traces: list[SessionTrace] = []
    events: list[TraceEvent] = []
    prefix = f"{codex_home}/"
    for path in paths:
        content = client.text(client.bash('cat -- "$1"', path))
        relative = path.removeprefix(prefix)
        traces.append(SessionTrace(path=relative, content=content))
        for sequence, line in enumerate(content.splitlines(), start=1):
            parsed = parse_json_line(line)
            if parsed:
                events.append(
                    trace_event_from_json(
                        parsed,
                        source=f"session_trace:{relative}",
                        sequence=sequence,
                    )
                )
    return traces, events


def _display_command(command: list[str]) -> str:
    return " ".join(
        f'"{argument}"' if any(character.isspace() for character in argument) else argument
        for argument in command
    )


def _display_argv(argv: list[str]) -> str:
    return _display_command(argv)


def _console_time(received_at: str | None) -> str:
    """Format a UTC receipt timestamp as local wall-clock time for progress display."""
    if not received_at:
        return "--:--:--"
    try:
        return datetime.fromisoformat(received_at).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"
