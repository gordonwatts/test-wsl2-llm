"""Windows-side orchestration of isolated Codex runs inside WSL2."""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

import yaml
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from test_wsl2_llm.models import (
    CommandResult,
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
from test_wsl2_llm.traces import (
    final_message_from_events,
    parse_json_line,
    trace_event_from_json,
    usage_from_events,
)

LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class WslClient:
    """Invoke one WSL distribution without shell-interpolating caller values."""

    def __init__(self, distro: str | None = None) -> None:
        self.distro = distro

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


def run_test(
    config: TestConfig, *, verbosity: int = 0, console: Console | None = None
) -> TestResult:
    """Execute one test and always return a reportable result after validation."""
    console = console or Console(stderr=True)
    client = WslClient(config.distro)
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
    retained = False
    runtime_marketplaces: list[str] = []
    resolved_parent = config.wsl_parent
    resolved_auth = config.auth_source
    model_information = ModelInformation(
        pricing_file=config.pricing_file or "bundled:model-pricing.yaml",
        currency="USD",
    )
    pricing_valid = False

    try:
        model_information = load_and_calculate_costs([], config.pricing_file)
        pricing_valid = True
        with state.phase("preflight"):
            codex_version = client.text(client.login_bash("codex --version")).strip()
            client.login_bash("codex plugin --help")
            resolved_parent = _resolve_wsl_path(client, config.wsl_parent, require_directory=True)
            resolved_auth = _resolve_wsl_path(client, config.auth_source)

        with state.phase("workspace_creation"):
            run_root = client.text(
                client.bash('mktemp -d -p "$1" test-wsl2-llm-XXXXXXXX', resolved_parent)
            ).strip()
            workspace_path = f"{run_root}/workspace"
            codex_home = f"{run_root}/.harness/codex-home"
            client.bash('mkdir -p "$1" "$2"', workspace_path, f"{run_root}/.harness/inputs")

        with state.phase("input_transfer"):
            _write_wsl_file(client, f"{run_root}/.harness/inputs/prompt.md", config.prompt)
            resolved_yaml = yaml.safe_dump(
                config.model_dump(mode="json"), sort_keys=False, allow_unicode=True
            )
            _write_wsl_file(client, f"{run_root}/.harness/inputs/config.yaml", resolved_yaml)
            runtime_marketplaces = _transfer_marketplaces(client, config.marketplaces, run_root)

        with state.phase("codex_home_setup"):
            client.bash(
                'mkdir -p "$1" && cp -- "$2" "$1/auth.json" && chmod 600 "$1/auth.json"',
                codex_home,
                resolved_auth,
            )
            _write_wsl_file(client, f"{codex_home}/config.toml", _codex_config(config))

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
            for installed_root in sorted(set(installed_plugin_roots)):
                found = client.bash(
                    'find "$1" -type f -name SKILL.md -printf "%h\\n" | sort -u',
                    installed_root,
                )
                skill_directories.extend(line for line in client.text(found).splitlines() if line)
            skill_directories = sorted(set(skill_directories))

        with state.phase("codex_execution"):
            codex_started = time.perf_counter()
            command_argv = client.command(
                client.shell_command(
                    'exec env CODEX_HOME="$1" codex exec --json --skip-git-repo-check '
                    '--model "$2" --config "$3" --cd "$4" -',
                    codex_home,
                    config.model,
                    f'model_reasoning_effort="{config.reasoning_effort}"',
                    workspace_path,
                    interactive_login=True,
                )
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
                progress_lines=config.progress_lines,
                verbosity=verbosity,
                console=console,
            )
            codex_seconds = time.perf_counter() - codex_started
            if exit_code:
                error = f"Codex exited with status {exit_code}"

        with state.phase("workspace_inventory"):
            files = _inventory(client, workspace_path)

        with state.phase("session_trace_collection"):
            session_traces, session_events = _session_traces(client, codex_home)
            trace_events.extend(session_events)

        retained = not config.cleanup
        if config.cleanup:
            client.bash('rm -rf -- "$1"', run_root)
            workspace_path = None
    except Exception as exc:  # A failure report is part of the public contract.
        error = str(exc)
        LOGGER.exception("WSL Codex test failed")
        files = []
        exit_code = exit_code or 1
        if workspace_path and run_root:
            try:
                with state.phase("workspace_inventory"):
                    files = _inventory(client, workspace_path)
                with state.phase("session_trace_collection"):
                    traces, session_events = _session_traces(client, codex_home)
                    session_traces.extend(traces)
                    trace_events.extend(session_events)
                retained = not config.cleanup
                if config.cleanup:
                    client.bash('rm -rf -- "$1"', run_root)
                    workspace_path = None
            except Exception as collection_error:
                error = f"{error}; result collection failed: {collection_error}"
    finally:
        if codex_home:
            client.bash('rm -f -- "$1/auth.json"', codex_home, check=False)

    finished_at = utc_now()
    usage = usage_from_events(parsed_events, config.model)
    if pricing_valid:
        model_information = load_and_calculate_costs(usage, config.pricing_file)
    final_message = final_message_from_events(parsed_events)
    return TestResult(
        prompt=config.prompt,
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
            distro=config.distro,
            workspace_path=workspace_path,
            workspace_retained=retained,
            codex_version=codex_version,
            error=error,
        ),
        timing=TimingResult(phases=state.phases, trace_events=trace_events),
        configuration=config.model_dump(mode="json"),
        usage=usage,
        model_information=model_information,
        result=FinalResult(final_message=final_message),
        workspace=WorkspaceResult(files=files),
        command=CommandResult(argv=command_argv),
        logs=LogsResult(
            stdout_jsonl=stdout,
            stderr=stderr,
            session_traces=session_traces,
        ),
    )


def _transfer_marketplaces(client: WslClient, sources: list[str], run_root: str) -> list[str]:
    runtime: list[str] = []
    marketplace_root = f"{run_root}/.harness/inputs/marketplaces"
    client.bash('mkdir -p "$1"', marketplace_root)
    for index, source in enumerate(sources, start=1):
        windows_path = Path(source)
        if windows_path.exists():
            destination = f"{marketplace_root}/marketplace-{index:03d}"
            mounted = client.text(
                client.bash('wslpath -a "$1"', str(windows_path.resolve()))
            ).strip()
            client.bash('mkdir -p "$1" && cp -a -- "$2"/. "$1"/', destination, mounted)
            runtime.append(destination)
        elif _is_git_marketplace_source(source):
            destination = f"{marketplace_root}/marketplace-{index:03d}"
            client.login_bash('git clone --depth 1 -- "$1" "$2"', source, destination)
            runtime.append(destination)
        else:
            runtime.append(source)
    return runtime


def _is_git_marketplace_source(source: str) -> bool:
    """Recognize URL and scp-style Git repository sources."""
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "ssh", "git"} and bool(parsed.netloc):
        return True
    return bool(re.fullmatch(r"[^@\s]+@[^:\s]+:.+", source))


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
    return "\n".join(
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


def _write_wsl_file(client: WslClient, path: str, content: str) -> None:
    client.bash('mkdir -p "$(dirname "$1")" && cat > "$1"', path, input_bytes=content.encode())


def _resolve_wsl_path(client: WslClient, path: str, *, require_directory: bool = False) -> str:
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
    progress_lines: int,
    verbosity: int,
    console: Console,
) -> tuple[int, str, str, list[TraceEvent], list[dict[str, Any]]]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(prompt)
    process.stdin.close()
    messages: queue.Queue[tuple[str, str, str, float] | tuple[str, None, None, None]] = (
        queue.Queue()
    )
    started = time.perf_counter()

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
    completed_streams = 0

    def consume(live: Live | None) -> None:
        nonlocal completed_streams
        while completed_streams < 2:
            stream, line, received_at, elapsed = messages.get()
            if line is None:
                completed_streams += 1
                continue
            sequences[stream] += 1
            raw[stream].append(line)
            parsed = parse_json_line(line)
            event_type = parsed.get("type") if parsed else None
            if parsed:
                parsed_events.append(parsed)
                trace_events.append(
                    trace_event_from_json(
                        parsed,
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
            display = f"{_console_time(received_at)} [{stream}] {event_type or line.rstrip()}"
            recent.append(display)
            del recent[:-progress_lines]
            if verbosity >= 2:
                LOGGER.debug("[%s] %s", stream, line.rstrip("\r\n"))
            elif live:
                live.update(Panel("\n".join(recent), title="Codex progress"))

    if verbosity >= 2:
        consume(None)
    else:
        with Live(
            Panel("Starting Codex...", title="Codex progress"),
            console=console,
            refresh_per_second=8,
        ) as live:
            consume(live)
    for thread in threads:
        thread.join()
    return (
        process.wait(),
        "".join(raw["stdout"]),
        "".join(raw["stderr"]),
        trace_events,
        parsed_events,
    )


def _inventory(client: WslClient, workspace: str) -> list[WorkspaceFile]:
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
    client: WslClient, codex_home: str | None
) -> tuple[list[SessionTrace], list[TraceEvent]]:
    if not codex_home:
        return [], []
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


def _console_time(received_at: str | None) -> str:
    """Format a UTC receipt timestamp as local wall-clock time for progress display."""
    if not received_at:
        return "--:--:--"
    try:
        return datetime.fromisoformat(received_at).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"
