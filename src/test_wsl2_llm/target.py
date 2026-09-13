"""Execution-target contracts used by the harness.

The runner deliberately depends on this small contract rather than on a
particular operating-system launcher.  The WSL implementation remains in
``runner`` for now; keeping the contract here makes adding another target a
separate change instead of coupling it to Codex orchestration.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
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


__all__ = ["ExecutionTarget"]
