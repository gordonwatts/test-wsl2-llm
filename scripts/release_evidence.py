"""Validate and sanitize release-candidate result evidence.

Live smokes remain opt-in because they need credentials and may spend model
tokens. This tool turns result YAML reports into a reviewable evidence bundle
without copying authentication, host paths, or raw workspace details into a PR.
"""

from __future__ import annotations

import argparse
import datetime as dt
import platform
import re
from pathlib import Path
from typing import Any

import yaml

from test_wsl2_llm import __version__
from test_wsl2_llm.compatibility import load_result_yaml, serialize_result
from test_wsl2_llm.report import render_markdown

REQUIRED_SCENARIOS = {"success", "validation-failure", "timeout", "retained-continuation"}
_SENSITIVE_KEY = re.compile(r"(?:auth|token|secret|password|credential|api[_-]?key)", re.I)
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"']+")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:tmp|home|Users|mnt|var|run)/[^\s<>\"']+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:token|secret|password|api[_-]?key|authorization)\s*[=:]\s*)([^\s,;]+)"
)


def sanitize_text(value: str) -> str:
    """Redact secret-like assignments and machine-specific paths."""
    value = _SECRET_ASSIGNMENT.sub(r"\1<redacted>", value)
    value = _WINDOWS_PATH.sub("<host-path>", value)
    return _POSIX_PATH.sub("<target-path>", value)


def sanitize(value: Any, *, key: str | None = None) -> Any:
    """Recursively sanitize a result-shaped value while retaining diagnostics."""
    if key and _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {name: sanitize(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def artifact_manifest(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep artifact identity without copying artifact contents."""
    manifest: list[dict[str, Any]] = []
    for item in data.get("copied_back", []):
        if not isinstance(item, dict):
            continue
        manifest.append(
            {
                "name": Path(str(item.get("source", ""))).name or "unknown",
                "type": item.get("type", "unknown"),
                "size": item.get("size", 0),
                "error": item.get("error"),
            }
        )
    for source in data.get("missing_copy_back", []):
        manifest.append({"name": Path(str(source)).name, "missing": True})
    return manifest


def check_scenario(name: str, data: dict[str, Any]) -> None:
    """Reject a report that cannot prove the named evidence scenario."""
    run = data.get("run", {})
    validation = data.get("validation", [])
    status = run.get("status")
    timed_out = bool(run.get("timed_out")) or bool(data.get("result", {}).get("timed_out"))
    if name == "success" and status != "succeeded":
        raise ValueError("success evidence must have run.status=succeeded")
    if name == "validation-failure" and (
        status != "failed"
        or not validation
        or not any(item.get("passed") is False for item in validation if isinstance(item, dict))
    ):
        raise ValueError(
            "validation-failure evidence must be failed and contain a failed validation check"
        )
    if name == "timeout" and (status != "failed" or not timed_out):
        raise ValueError("timeout evidence must be failed with timed_out=true")
    if name == "retained-continuation" and (
        status != "succeeded" or not data.get("continued_from")
    ):
        raise ValueError("retained-continuation evidence must be a successful continuation")


def version_record(data: dict[str, Any]) -> dict[str, Any]:
    run = data.get("run", {})
    provenance = data.get("provenance") or {}
    return {
        "harness": provenance.get("harness_version") or __version__,
        "python": platform.python_version(),
        "platform": platform.platform(aliased=True),
        "agent": provenance.get(
            "agent", run.get("agent", data.get("configuration", {}).get("agent", "unknown"))
        ),
        "agent_version": provenance.get("agent_version")
        or run.get("agent_version")
        or run.get("codex_version")
        or "unknown",
        "target": provenance.get("target") or run.get("target", "unknown"),
        "target_version": provenance.get("target_version", "unknown"),
        "distribution": run.get("distro"),
    }


def build_bundle(results: dict[str, Path], output: Path) -> None:
    """Write sanitized scenario reports, versions, and artifact manifests."""
    missing = REQUIRED_SCENARIOS - results.keys()
    if missing:
        raise ValueError(f"missing required scenarios: {', '.join(sorted(missing))}")
    unexpected = results.keys() - REQUIRED_SCENARIOS
    if unexpected:
        raise ValueError(f"unexpected scenarios: {', '.join(sorted(unexpected))}")
    output.mkdir(parents=True, exist_ok=True)
    scenario_dir = output / "scenarios"
    scenario_dir.mkdir(exist_ok=True)
    versions: dict[str, Any] = {}
    summary: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "harness_version": __version__,
        "scenarios": {},
    }
    for name, path in sorted(results.items()):
        result = load_result_yaml(path)
        data = serialize_result(result)
        check_scenario(name, data)
        sanitized = sanitize(data)
        (scenario_dir / f"{name}.yaml").write_text(
            yaml.safe_dump(sanitized, sort_keys=False, allow_unicode=True, width=1000),
            encoding="utf-8",
        )
        (scenario_dir / f"{name}.md").write_text(
            sanitize_text(render_markdown(result, report_path=scenario_dir / f"{name}.md")),
            encoding="utf-8",
        )
        (scenario_dir / f"{name}.artifacts.yaml").write_text(
            yaml.safe_dump(sanitize(artifact_manifest(data)), sort_keys=False), encoding="utf-8"
        )
        versions[name] = version_record(data)
        summary["scenarios"][name] = {
            "source": path.name,
            "status": data["run"]["status"],
            "target": data["run"].get("target", "unknown"),
            "agent": data["run"].get("agent", "unknown"),
            "evidence": f"scenarios/{name}.yaml",
        }
    (output / "versions.yaml").write_text(
        yaml.safe_dump(versions, sort_keys=False), encoding="utf-8"
    )
    (output / "summary.yaml").write_text(yaml.safe_dump(summary, sort_keys=False), encoding="utf-8")


def parse_result(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or name not in REQUIRED_SCENARIOS or not path:
        choices = ", ".join(sorted(REQUIRED_SCENARIOS))
        raise argparse.ArgumentTypeError(f"result must be SCENARIO=PATH; choices: {choices}")
    return name, Path(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="evidence bundle directory")
    parser.add_argument(
        "--result",
        action="append",
        required=True,
        type=parse_result,
        metavar="SCENARIO=YAML",
        help="repeat for success, validation-failure, timeout, and retained-continuation",
    )
    args = parser.parse_args(argv)
    names = [name for name, _ in args.result]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        parser.error(f"duplicate scenarios: {', '.join(duplicates)}")
    try:
        build_bundle(dict(args.result), args.output)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
