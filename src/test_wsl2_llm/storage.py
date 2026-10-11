"""Read-only capacity checks before launching agents on Linux hosts."""

import inspect
import json
from typing import Any

MIN_AVAILABLE_BYTES = 1_000_000_000  # Decimal 1 GB, available to the current user.


class StoragePreflightError(OSError):
    """Host storage is unhealthy or could not be checked safely."""


def inspect_storage(parent: str) -> list[dict[str, Any]]:
    """Inspect /tmp and the parent without creating files or directories.

    This function is also shipped as source to a remote Python interpreter.
    Keep its dependencies in the standard-library imports inside the function.
    """
    import os
    import pwd

    if parent == "~" or parent.startswith("~/"):
        parent = pwd.getpwuid(os.getuid()).pw_dir + parent[1:]
    entries = []
    seen = set()
    for directory in ("/tmp", parent):
        directory = os.path.realpath(directory)
        if directory in seen:
            continue
        seen.add(directory)
        measured = directory
        while True:
            try:
                os.stat(measured)
                break
            except FileNotFoundError:
                if directory == os.path.realpath("/tmp"):
                    raise OSError("Storage check requires an existing /tmp directory") from None
                ancestor = os.path.dirname(measured)
                if ancestor == measured:
                    raise OSError(f"No existing ancestor for directory {directory!r}") from None
                measured = ancestor
        if not os.path.isdir(measured):
            raise OSError(f"Storage check requires a directory: {measured!r}")
        capacity = os.statvfs(measured)
        entries.append(
            {
                "directory": directory,
                "measured_directory": measured,
                "available_bytes": capacity.f_bavail * (capacity.f_frsize or capacity.f_bsize),
                "available_inodes": capacity.f_favail,
            }
        )
    return entries


def storage_probe_script() -> str:
    """Use the same statvfs implementation on WSL and remote Linux hosts."""
    return (
        "import json, sys\nfrom typing import Any\n"
        + inspect.getsource(inspect_storage)
        + "\nprint(json.dumps(inspect_storage(sys.argv[1])))\n"
    )


def check_storage(target: object, parent: str) -> None:
    """Raise before agent launch when a supported target lacks storage capacity."""
    # Native Windows and injected/custom targets without this optional capability
    # retain their existing behavior, just like other native target hooks.
    probe = getattr(type(target), "storage_capacity", None)
    if probe is None:
        return
    try:
        entries = target.storage_capacity(parent)  # type: ignore[attr-defined]
        failures = []
        for entry in entries:
            available_bytes = entry["available_bytes"]
            available_inodes = entry["available_inodes"]
            if available_bytes >= MIN_AVAILABLE_BYTES and available_inodes > 0:
                continue
            directory = entry["directory"]
            measured = entry["measured_directory"]
            location = repr(directory)
            if measured != directory:
                location += f" (measured at existing ancestor {measured!r})"
            failures.append(
                f"{location}: available {available_bytes:,} bytes; required at least "
                f"{MIN_AVAILABLE_BYTES:,} bytes (1 GB); available {available_inodes:,} "
                "inodes; required at least 1 inode"
            )
    except Exception as exc:
        raise StoragePreflightError(
            f"Storage preflight could not inspect /tmp and workspace parent {parent!r}: {exc}"
        ) from exc
    if failures:
        raise StoragePreflightError(
            "Storage preflight failed; fix host storage before running agents. "
            + "; ".join(failures)
        )


def remote_storage_capacity(target: object, parent: str) -> list[dict[str, Any]]:
    completed = target.bash(  # type: ignore[attr-defined]
        'python3 -c "$1" "$2"', storage_probe_script(), parent, check=False
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(f"Linux storage probe exited {completed.returncode}: {detail}")
    return json.loads(completed.stdout)
