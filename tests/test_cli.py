from pathlib import Path

import yaml
from typer.testing import CliRunner

from test_wsl2_llm.cli import app

runner = CliRunner()


def test_help_documents_run_and_core_options() -> None:
    top = runner.invoke(app, ["--help"])
    run = runner.invoke(app, ["run", "--help"])
    assert top.exit_code == 0
    assert "run" in top.stdout
    assert run.exit_code == 0
    for option in (
        "--prompt",
        "--config",
        "--force",
        "--marketplace",
        "--output",
        "--pricing-file",
        "--save-config",
        "--title",
    ):
        assert option in run.stdout
    assert "--overwrite" not in run.stdout


def test_config_only_writes_resolved_yaml_without_wsl(tmp_path: Path) -> None:
    destination = tmp_path / "saved.yaml"
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--save-config",
            str(destination),
            "--config-only",
        ],
    )
    assert result.exit_code == 0, result.stdout
    saved = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert saved["prompt"] == "hello"
    assert saved["title"] == "# WSL2 Codex test result"
    assert not (tmp_path / "out.yaml").exists()


def test_force_maps_to_overwrite_in_saved_configuration(tmp_path: Path) -> None:
    destination = tmp_path / "saved.yaml"
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--force",
            "--save-config",
            str(destination),
            "--config-only",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert yaml.safe_load(destination.read_text(encoding="utf-8"))["overwrite"] is True


def test_config_only_requires_save_config(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
            "--config-only",
        ],
    )
    assert result.exit_code == 2
    assert "requires --save-config" in result.output


def test_existing_result_is_rejected_before_wsl(tmp_path: Path) -> None:
    (tmp_path / "out.md").write_text("existing", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run",
            "--prompt",
            "hello",
            "--model",
            "gpt-test",
            "--output",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "already exists" in result.output
