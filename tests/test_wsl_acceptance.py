from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path, PurePosixPath

import pytest
import yaml
from typer.testing import CliRunner

from test_wsl2_llm.cli import app

pytestmark = pytest.mark.wsl_acceptance
runner = CliRunner()


def _wsl(
    distro: str, arguments: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["wsl.exe", "-d", distro, "--", *arguments],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _cleanup_workspace(distro: str, workspace: str | None) -> None:
    if not workspace:
        return
    run_root = str(PurePosixPath(workspace).parent)
    if not PurePosixPath(run_root).name.startswith("test-wsl2-llm-"):
        raise AssertionError(f"refusing to remove unexpected path: {run_root}")
    _wsl(distro, ["rm", "-rf", "--", run_root], check=False)


@pytest.mark.timeout(900)
def test_atlas_al9_hello_world(tmp_path: Path, pytestconfig: pytest.Config) -> None:
    distro = "atlas_al9"
    model = pytestconfig.getoption("--wsl-model")
    output = tmp_path / "hello-run"
    workspace = None
    try:
        result = runner.invoke(
            app,
            [
                "run",
                "--distro",
                distro,
                "--model",
                model,
                "--prompt",
                "Create hello.txt with the exact content: Hello from Codex on atlas_al9",
                "--output",
                str(output),
            ],
            catch_exceptions=False,
        )
        result_yaml = output.with_suffix(".yaml")
        if result_yaml.exists():
            data = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            workspace = data["run"]["workspace_path"]
        assert result.exit_code == 0, result.output
        markdown = output.with_suffix(".md")
        assert markdown.exists() and result_yaml.exists()
        assert data["skills"] == {"marketplaces": [], "plugins": [], "directories": []}
        assert data["run"]["total_duration_seconds"] > 0
        assert data["run"]["codex_execution_seconds"] > 0
        assert data["timing"]["phases"]
        assert data["timing"]["trace_events"]
        assert data["usage"]
        assert data["logs"]["session_traces"]
        assert any(item["path"] == "hello.txt" for item in data["workspace"]["files"])
        assert "hello.txt" in markdown.read_text(encoding="utf-8")
        content = _wsl(distro, ["cat", f"{workspace}/hello.txt"]).stdout
        assert content.rstrip("\r\n") == "Hello from Codex on atlas_al9"
    finally:
        _cleanup_workspace(distro, workspace)


@pytest.mark.timeout(900)
def test_atlas_al9_transfers_and_uses_skill(tmp_path: Path, pytestconfig: pytest.Config) -> None:
    distro = "atlas_al9"
    model = pytestconfig.getoption("--wsl-model")
    sentinel = f"wsl-transfer-proof-{uuid.uuid4()}"
    marketplace = _make_marketplace(tmp_path, sentinel)
    output = tmp_path / "skill-run"
    input_config = tmp_path / "skill-test.yaml"
    input_config.write_text(
        yaml.safe_dump(
            {
                "prompt": (
                    "Use $wsl-transfer-proof. Follow its instructions to create "
                    "skill-proof.txt in the current workspace."
                ),
                "model": model,
                "distro": distro,
                "output": str(output),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    workspace = None
    try:
        result = runner.invoke(
            app,
            [
                "run",
                "--config",
                str(input_config),
                "--marketplace",
                str(marketplace),
                "--plugin",
                "wsl-transfer-proof@wsl-transfer-test",
            ],
            catch_exceptions=False,
        )
        result_yaml = output.with_suffix(".yaml")
        if result_yaml.exists():
            data = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            workspace = data["run"]["workspace_path"]
        assert result.exit_code == 0, result.output
        markdown = output.with_suffix(".md").read_text(encoding="utf-8")
        run_root = str(PurePosixPath(workspace).parent)
        copied = (
            f"{run_root}/.harness/inputs/marketplaces/marketplace-001/"
            "plugins/wsl-transfer-proof/skills/wsl-transfer-proof/references/sentinel.txt"
        )
        assert _wsl(distro, ["test", "-f", copied], check=False).returncode == 0
        assert all(not directory.startswith("/mnt/") for directory in data["skills"]["directories"])
        assert data["skills"]["plugins"] == ["wsl-transfer-proof@wsl-transfer-test"]
        assert data["timing"]["trace_events"]
        assert data["usage"]
        assert data["logs"]["session_traces"]
        assert "wsl-transfer-proof" in markdown
        proof = _wsl(distro, ["cat", f"{workspace}/skill-proof.txt"]).stdout
        assert proof.rstrip("\r\n") == sentinel
    finally:
        _cleanup_workspace(distro, workspace)


def _make_marketplace(root: Path, sentinel: str) -> Path:
    marketplace = root / "marketplace"
    plugin = marketplace / "plugins" / "wsl-transfer-proof"
    skill = plugin / "skills" / "wsl-transfer-proof"
    references = skill / "references"
    manifest_dir = plugin / ".codex-plugin"
    marketplace_manifest = marketplace / ".agents" / "plugins"
    references.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    marketplace_manifest.mkdir(parents=True)
    (references / "sentinel.txt").write_text(sentinel + "\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        """---
name: wsl-transfer-proof
description: Prove that a Windows marketplace skill and reference reached WSL.
---

# WSL transfer proof

Read `references/sentinel.txt`. Create `skill-proof.txt` in the current workspace with
exactly the sentinel text, excluding the reference file's final newline. Do not invent or
transform the value.
""",
        encoding="utf-8",
    )
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "wsl-transfer-proof",
                "version": "1.0.0",
                "description": "Acceptance fixture for Windows-to-WSL skill transfer",
                "author": {"name": "test-wsl2-llm"},
                "skills": "./skills/",
                "interface": {
                    "displayName": "WSL Transfer Proof",
                    "shortDescription": "Proves that a transferred skill can read its reference",
                    "longDescription": "Temporary acceptance fixture for test-wsl2-llm.",
                    "developerName": "test-wsl2-llm",
                    "category": "Developer Tools",
                    "capabilities": ["Write"],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (marketplace_manifest / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "wsl-transfer-test",
                "interface": {"displayName": "WSL Transfer Test"},
                "plugins": [
                    {
                        "name": "wsl-transfer-proof",
                        "source": {"source": "local", "path": "./plugins/wsl-transfer-proof"},
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                        "category": "Developer Tools",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return marketplace
