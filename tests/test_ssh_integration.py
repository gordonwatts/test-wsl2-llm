"""Real SSH-target coverage using a disposable Docker OpenSSH server."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from getpass import getuser
from pathlib import Path

import pytest

from test_wsl2_llm.target import SshTarget

pytestmark = pytest.mark.ssh_integration


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, **kwargs)


def _restrict_private_key_permissions(path: Path) -> None:
    if os.name == "nt":
        _run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getuser()}:F"],
            stdout=subprocess.PIPE,
        )
    else:
        path.chmod(0o600)


@pytest.fixture(scope="module")
def ssh_target(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SshTarget]:
    """Build and start the fixture server with isolated SSH credentials."""
    try:
        _run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("Docker is required for SSH integration tests")

    root = Path(__file__).parent / "fixtures" / "ssh_server"
    key_dir = tmp_path_factory.mktemp("ssh-key")
    private_key = key_dir / "id_ed25519"
    _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)])
    public_key = private_key.with_name("id_ed25519.pub").read_text(encoding="utf-8").strip()

    image = f"test-wsl2-llm-ssh:{os.getpid()}"
    _run(["docker", "build", "-t", image, str(root)], stdout=subprocess.PIPE)
    container = _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-e",
            f"AUTHORIZED_KEY={public_key}",
            "-p",
            "127.0.0.1::22",
            image,
        ],
        stdout=subprocess.PIPE,
    ).stdout.strip()

    try:
        port = None
        for _ in range(30):
            mapped = subprocess.run(
                ["docker", "port", container, "22/tcp"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if mapped:
                port = int(mapped.rsplit(":", 1)[1])
                break
            time.sleep(0.2)
        if port is None:
            raise RuntimeError("Docker did not publish the SSH fixture port")

        home = tmp_path_factory.mktemp("ssh-home")
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir()
        isolated_key = ssh_dir / "id_ed25519"
        isolated_key.write_bytes(private_key.read_bytes())
        _restrict_private_key_permissions(isolated_key)
        known_hosts = ssh_dir / "known_hosts"
        for _ in range(30):
            scan = subprocess.run(
                ["ssh-keyscan", "-p", str(port), "127.0.0.1"],
                check=False,
                capture_output=True,
                text=True,
            )
            if scan.stdout:
                known_hosts.write_text(scan.stdout, encoding="utf-8")
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Docker SSH fixture did not publish a host key")

        environment = os.environ.copy()
        environment.update({"HOME": str(home), "USERPROFILE": str(home)})
        target = SshTarget(
            "127.0.0.1",
            user="harness",
            port=port,
            remote_workspace_parent="/tmp/test-wsl2-llm-runs",
            source_environment=environment,
            identity_file=str(isolated_key),
            known_hosts_file=str(known_hosts),
        )
        for _ in range(30):
            try:
                target.run(["true"])
                break
            except RuntimeError:
                time.sleep(0.2)
        else:
            raise RuntimeError("Docker SSH fixture did not accept connections")
        target.run(["mkdir", "-p", "/tmp/test-wsl2-llm-runs"])

        yield target
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        subprocess.run(["docker", "rmi", image], check=False, capture_output=True)


@pytest.fixture
def running_ssh_target(ssh_target: SshTarget) -> Iterator[SshTarget]:
    yield ssh_target
    for run_root in list(ssh_target._owned_workspace_tokens):
        ssh_target.cleanup_workspace(run_root)


def test_real_ssh_transfer_and_cleanup(running_ssh_target: SshTarget, tmp_path: Path) -> None:
    source = tmp_path / "input file café.txt"
    source.write_text("hello over real ssh\\n", encoding="utf-8")
    run_root = running_ssh_target.create_workspace("/tmp/test-wsl2-llm-runs")

    running_ssh_target.copy_to_target(str(source), f"{run_root}/workspace")
    running_ssh_target.run(["test", "-f", f"{run_root}/workspace/{source.name}"])
    destination = tmp_path / "copied.txt"
    running_ssh_target.copy_from_target(f"{run_root}/workspace/{source.name}", str(destination))

    assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    running_ssh_target.cleanup_workspace(run_root)
    assert run_root not in running_ssh_target._owned_workspace_tokens


def test_real_ssh_cancellation_stops_owned_process(running_ssh_target: SshTarget) -> None:
    run_root = running_ssh_target.create_workspace("/tmp/test-wsl2-llm-runs")
    process = running_ssh_target.start_process(
        running_ssh_target.command(["fake-codex", "--sleep", "60"]),
    )
    try:
        time.sleep(0.5)
        running_ssh_target.stop_process(process)
        assert process.poll() is not None
        remaining = running_ssh_target.run(
            ["find", run_root, "-name", "ssh-owner-*.pid", "-print"],
        )
        assert not remaining.stdout.strip()
    finally:
        if process.poll() is None:
            running_ssh_target.stop_process(process)
