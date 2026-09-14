"""Execution-target contracts used by the harness.

The runner deliberately depends on this small contract rather than on a
particular operating-system launcher.  The WSL implementation remains in
``runner`` for now; keeping the contract here makes adding another target a
separate change instead of coupling it to Codex orchestration.
"""

from __future__ import annotations

import math
import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class ExecutionTarget(Protocol):
    """Operations required by the harness from an execution environment.

    Paths passed to ``run``/``bash`` and the workspace methods are native to
    the target.  Host paths are accepted only by the explicit transfer
    methods, which keeps path conversion and quoting in the target adapter.
    """

    environment: Mapping[str, str]
    distro: str | None

    def command(self, arguments: list[str]) -> list[str]: ...

    def run(
        self,
        arguments: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]: ...

    def bash(
        self,
        script: str,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]: ...

    def login_bash(
        self,
        script: str,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]: ...

    def shell_command(
        self, script: str, *arguments: str, interactive_login: bool = False
    ) -> list[str]: ...

    def text(self, completed: subprocess.CompletedProcess[bytes]) -> str: ...

    def start_process(
        self, command: list[str], *, stdin: int, stdout: int, stderr: int
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
    """Noninteractive command execution through the user's OpenSSH config."""

    host: str
    user: str | None = None
    port: int | None = None
    remote_workspace_parent: str = "/tmp"
    connect_timeout_seconds: float = 10.0
    source_environment: Mapping[str, str] | None = None

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
        self, script: str, input_bytes: bytes | None, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["ssh", *self._ssh_options(), self.destination, self._remote_command(script)]
        completed = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=self.connect_timeout_seconds,
            env=self.environment,
        )
        if check and completed.returncode:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"SSH command failed ({completed.returncode}): {stderr}")
        return completed

    def command(self, arguments: list[str]) -> list[str]:
        if not arguments:
            raise ValueError("SSH command must contain an executable")
        return [
            "ssh",
            *self._ssh_options(),
            self.destination,
            self._remote_command(f"exec {shlex.join(arguments)}"),
        ]

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
        if getattr(process, "poll", lambda: None)() is not None:
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

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
