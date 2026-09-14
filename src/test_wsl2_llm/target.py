"""Execution-target contracts used by the harness.

The runner deliberately depends on this small contract rather than on a
particular operating-system launcher. The WSL implementation remains in
runner for now; keeping the contract here makes adding another target a
separate change instead of coupling it to Codex orchestration.
"""

from __future__ import annotations

import io
import math
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ExecutionTarget(Protocol):
    """Operations required by the harness from an execution environment."""

    environment: Mapping[str, str]
    distro: str | None

    def command(self, arguments: list[str]) -> list[str]: ...
    def run(
        self, arguments: list[str], *, input_bytes: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]: ...
    def bash(
        self, script: str, *arguments: str, input_bytes: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]: ...
    def login_bash(
        self, script: str, *arguments: str, input_bytes: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]: ...
    def shell_command(
        self, script: str, *arguments: str, interactive_login: bool = False
    ) -> list[str]: ...
    def text(self, completed: subprocess.CompletedProcess[bytes]) -> str: ...
    def start_process(
        self,
        command: list[str],
        *,
        stdin: int = subprocess.PIPE,
        stdout: int = subprocess.PIPE,
        stderr: int = subprocess.PIPE,
    ) -> subprocess.Popen[str]: ...
    def stop_process(self, process: subprocess.Popen[str]) -> None: ...
    def to_target_path(self, host_path: str) -> str: ...
    def copy_to_target(
        self, host_path: str, target_directory: str, *, recursive: bool = False
    ) -> None: ...
    def copy_from_target(self, target_path: str, host_path: str) -> None: ...
    def create_workspace(self, parent: str) -> str: ...
    def cleanup_workspace(self, run_root: str) -> None: ...


@dataclass(frozen=True)
class SshTarget:
    """Linux destination reached through a passwordless OpenSSH configuration.

    Transfers use tar over the existing SSH command path. The target writes a
    per-run ownership marker and a per-process PID file below that run root.
    Those markers are checked before cancellation or cleanup can remove data.
    """

    host: str
    user: str | None = None
    port: int | None = None
    remote_workspace_parent: str = "/tmp"
    connect_timeout_seconds: float = 10.0
    source_environment: Mapping[str, str] | None = None
    _owned_workspace_tokens: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _process_owner_files: dict[int, str] = field(default_factory=dict, init=False, repr=False)
    _pending_owner_files: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.host.strip() or any(character.isspace() for character in self.host):
            raise ValueError("SSH host must be a non-empty alias or hostname without whitespace")
        if self.user is not None and (
            not self.user.strip() or any(character.isspace() for character in self.user)
        ):
            raise ValueError("SSH user must be a non-empty name without whitespace")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        if not self.remote_workspace_parent.strip():
            raise ValueError("remote workspace parent must not be empty")
        if not math.isfinite(self.connect_timeout_seconds) or self.connect_timeout_seconds <= 0:
            raise ValueError("SSH connect timeout must be finite and greater than zero")

    @property
    def environment(self) -> Mapping[str, str]:
        return dict(os.environ if self.source_environment is None else self.source_environment)

    @property
    def distro(self) -> None:
        return None

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _ssh_options(self) -> list[str]:
        options = [
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={math.ceil(self.connect_timeout_seconds)}",
        ]
        if self.port is not None:
            options.extend(["-p", str(self.port)])
        return options

    @staticmethod
    def _remote_command(script: str) -> str:
        return shlex.join(["bash", "-lc", script])

    @staticmethod
    def _script_with_arguments(script: str, arguments: tuple[str, ...]) -> str:
        if not script:
            raise ValueError("SSH remote script must not be empty")
        prefix = f"set -- {shlex.join(list(arguments))}; " if arguments else ""
        return prefix + script

    def _invoke(
        self, script: str, input_bytes: bytes | None, check: bool, *, timeout: float | None = ...
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["ssh", *self._ssh_options(), self.destination, self._remote_command(script)]
        completed = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=self.connect_timeout_seconds if timeout is ... else timeout,
            env=self.environment,
        )
        if check and completed.returncode:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"SSH command failed ({completed.returncode}): {stderr}")
        return completed

    def command(self, arguments: list[str]) -> list[str]:
        if not arguments:
            raise ValueError("SSH command must contain an executable")
        command = shlex.join(arguments)
        run_root = next(iter(self._owned_workspace_tokens), "/tmp")
        owner_file = f"{run_root}/.harness/ssh-owner-{uuid.uuid4().hex}.pid"
        wrapper = "\\n".join(
            [
                f"owner_file={shlex.quote(owner_file)}",
                'mkdir -p -- "$(dirname -- "$owner_file")"',
                f"setsid sh -c {shlex.quote(f'exec {command}')} &",
                "child=$!",
                'printf \'%s\\n\' "$child" > "$owner_file"',
                "cleanup() {",
                '  kill -TERM -- "-$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null || true',
                '  rm -f -- "$owner_file"',
                "}",
                "trap cleanup INT TERM",
                'wait "$child"; status=$?',
                'rm -f -- "$owner_file"',
                'exit "$status"',
            ]
        )
        command_line = self._remote_command(wrapper)
        self._pending_owner_files[command_line] = owner_file
        return ["ssh", *self._ssh_options(), self.destination, command_line]

    def run(
        self, arguments: list[str], *, input_bytes: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        if not arguments:
            raise ValueError("SSH command must contain an executable")
        return self._invoke(f"exec {shlex.join(arguments)}", input_bytes, check)

    def bash(
        self, script: str, *arguments: str, input_bytes: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        return self._invoke(self._script_with_arguments(script, arguments), input_bytes, check)

    def login_bash(
        self, script: str, *arguments: str, input_bytes: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        return self.bash(script, *arguments, input_bytes=input_bytes, check=check)

    def shell_command(
        self, script: str, *arguments: str, interactive_login: bool = False
    ) -> list[str]:
        del interactive_login
        return [
            "ssh",
            *self._ssh_options(),
            self.destination,
            self._remote_command(self._script_with_arguments(script, arguments)),
        ]

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
        process = subprocess.Popen(
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
        owner_file = self._pending_owner_files.pop(command[-1], None)
        if owner_file is not None:
            self._process_owner_files[id(process)] = owner_file
        return process

    def _stop_remote_owner(self, owner_file: str) -> None:
        script = """
owner_file="$1"
case "$owner_file" in */.harness/ssh-owner-*.pid) ;; *) exit 2 ;; esac
if test -s "$owner_file"; then
  pid=$(cat -- "$owner_file" 2>/dev/null || true)
  case "$pid" in
    ''|*[!0-9]*) ;;
    *) kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
       for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
       kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true ;;
  esac
fi
rm -f -- "$owner_file"
""".strip()
        self._invoke(self._script_with_arguments(script, (owner_file,)), None, False)

    def stop_process(self, process: subprocess.Popen[str]) -> None:
        owner_file = self._process_owner_files.pop(id(process), None)
        if owner_file is not None:
            with suppress(Exception, KeyboardInterrupt):
                self._stop_remote_owner(owner_file)
        if getattr(process, "poll", lambda: None)() is not None:
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def to_target_path(self, host_path: str) -> str:
        return str(Path(host_path).expanduser().resolve())

    @staticmethod
    def _archive_source(source: Path, *, recursive: bool) -> bytes:
        if not source.exists():
            raise FileNotFoundError(source)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.add(source, arcname="." if recursive else source.name, recursive=recursive)
        return stream.getvalue()

    @staticmethod
    def _extract_archive(data: bytes, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            root = destination.resolve()
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                if os.path.commonpath((str(root), str(target))) != str(root):
                    raise ValueError(f"SSH transfer archive contains an unsafe path: {member.name}")
            archive.extractall(destination)
        return destination

    def copy_to_target(
        self, host_path: str, target_directory: str, *, recursive: bool = False
    ) -> None:
        data = self._archive_source(Path(host_path).expanduser().resolve(), recursive=recursive)
        self._invoke(
            self._script_with_arguments(
                'mkdir -p -- "$1" && tar -x -f - -C "$1"', (target_directory,)
            ),
            data,
            True,
            timeout=None,
        )

    def copy_from_target(self, target_path: str, host_path: str) -> None:
        completed = self._invoke(
            self._script_with_arguments('tar -c -f - -- "$1"', (target_path,)),
            None,
            True,
            timeout=None,
        )
        destination = Path(host_path).expanduser().resolve()
        with tempfile.TemporaryDirectory(prefix="test-wsl2-llm-ssh-") as staging:
            extracted = self._extract_archive(completed.stdout, Path(staging))
            members = [path for path in extracted.rglob("*") if path.is_file() or path.is_symlink()]
            if len(members) != 1:
                raise RuntimeError(f"SSH copy-out expected one file, received {len(members)}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(members[0], destination)

    def create_workspace(self, parent: str) -> str:
        token = uuid.uuid4().hex
        script = """
parent="$1"
test -d "$parent"
run_root=$(mktemp -d -p "$parent" test-wsl2-llm-XXXXXXXX)
mkdir -p "$run_root/workspace" "$run_root/.harness/inputs"
printf '%s\\n' "$2" > "$run_root/.harness/ownership"
printf '%s\\n' "$run_root"
""".strip()
        run_root = self.text(
            self._invoke(self._script_with_arguments(script, (parent, token)), None, True)
        ).strip()
        if not run_root or not run_root.startswith("/"):
            raise RuntimeError("SSH workspace creation returned an invalid remote path")
        self._owned_workspace_tokens[run_root] = token
        return run_root

    def cleanup_workspace(self, run_root: str) -> None:
        token = self._owned_workspace_tokens.get(run_root)
        if token is None:
            raise ValueError("refusing to remove an SSH workspace not owned by this target")
        script = """
run_root="$1"
expected="$2"
test -d "$run_root"
test -f "$run_root/.harness/ownership"
test "$(cat -- "$run_root/.harness/ownership")" = "$expected"
case "$run_root" in */test-wsl2-llm-*) ;; *) exit 2 ;; esac
rm -rf -- "$run_root"
test ! -e "$run_root"
""".strip()
        try:
            self._invoke(self._script_with_arguments(script, (run_root, token)), None, True)
        except Exception as exc:
            raise RuntimeError(
                "remote workspace cleanup could not be confirmed; "
                f"workspace retained at {run_root}: {exc}"
            ) from exc
        self._owned_workspace_tokens.pop(run_root, None)

    def to_config(self) -> dict[str, str | int | float | None]:
        return {
            "target": "ssh",
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "remote_workspace_parent": self.remote_workspace_parent,
            "connect_timeout_seconds": self.connect_timeout_seconds,
        }


SshClient = SshTarget
SSHExecutionTarget = SshTarget

__all__ = ["ExecutionTarget", "SSHExecutionTarget", "SshClient", "SshTarget"]
