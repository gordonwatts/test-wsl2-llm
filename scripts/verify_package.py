"""Verify the files required for a distributable package are in its artifacts."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

REQUIRED = {"LICENSE", "template.schema.json", "model-pricing.yaml"}


def _check_names(names: set[str]) -> list[str]:
    return [
        required
        for required in REQUIRED
        if not any(name == required or name.endswith(f"/{required}") for name in names)
    ]


def verify(directory: Path) -> None:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            f"expected exactly one wheel and sdist in {directory}, "
            f"found {len(wheels)} wheels and {len(sdists)} sdists"
        )

    with zipfile.ZipFile(wheels[0]) as archive:
        missing = _check_names(set(archive.namelist()))
    if missing:
        raise ValueError(f"{wheels[0].name} is missing: {', '.join(sorted(missing))}")

    with tarfile.open(sdists[0], "r:gz") as archive:
        missing = _check_names({member.name for member in archive.getmembers()})
    if missing:
        raise ValueError(f"{sdists[0].name} is missing: {', '.join(sorted(missing))}")

    print(f"Verified package data in {wheels[0].name} and {sdists[0].name}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/verify_package.py DIST_DIRECTORY")
    verify(Path(sys.argv[1]).resolve())
