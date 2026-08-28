"""Load, merge, normalize, and save test configuration."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from test_wsl2_llm.models import TestConfig

LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG_ENV = "TEST_WSL2_LLM_DEFAULT_CONFIG"


def default_config_path() -> Path:
    """Return the optional user defaults path, allowing an explicit environment override."""
    override = os.environ.get(DEFAULT_CONFIG_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".test-wsl2-llm" / "config.yaml"


def load_default_config() -> dict[str, Any]:
    """Load user defaults when present and report discovery at INFO level."""
    path = default_config_path()
    if not path.is_file():
        return {}
    LOGGER.info("Loading default configuration: %s", path)
    return load_config_file(path)


def merge_config_values(
    base: dict[str, Any], override: dict[str, Any]
) -> dict[str, Any]:
    """Merge a higher-precedence partial config, including nested environment fields."""
    merged = dict(base)
    for key, value in override.items():
        if key == "environment" and isinstance(value, dict):
            environment = dict(merged.get("environment", {}))
            environment.update(value)
            merged[key] = environment
        else:
            merged[key] = value
    return merged


def load_config_file(path: Path) -> dict[str, Any]:
    """Load YAML and resolve file-bearing values relative to the YAML file."""
    path = path.resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration YAML must contain a mapping")
    base = path.parent
    if raw.get("prompt_file") is not None:
        if raw.get("prompt") is not None:
            raise ValueError("configuration may contain prompt or prompt_file, not both")
        prompt_path = _resolve_windows_path(str(raw.pop("prompt_file")), base)
        raw["prompt"] = prompt_path.read_text(encoding="utf-8")
    if "marketplaces" in raw:
        raw["marketplaces"] = [
            _normalize_marketplace(str(source), base) for source in raw["marketplaces"]
        ]
    if "copy_files" in raw:
        raw["copy_files"] = [
            str(_resolve_windows_path(str(source), base)) for source in raw["copy_files"]
        ]
    if "copy_back" in raw:
        raw["copy_back"] = [str(source) for source in raw["copy_back"]]
    if raw.get("output"):
        raw["output"] = str(_resolve_windows_path(str(raw["output"]), base))
    if raw.get("pricing_file"):
        raw["pricing_file"] = str(_resolve_windows_path(str(raw["pricing_file"]), base))
    return raw


def build_config(
    file_values: dict[str, Any], cli_values: dict[str, Any], cwd: Path | None = None
) -> TestConfig:
    """Merge explicit CLI values over YAML and apply deterministic defaults."""
    cwd = (cwd or Path.cwd()).resolve()
    merged = dict(file_values)
    merged.update({key: value for key, value in cli_values.items() if value is not None})
    if cli_values.get("environment") is not None:
        environment = dict(file_values.get("environment", {}))
        environment.update(
            {
                key: value
                for key, value in cli_values["environment"].items()
                if value is not None
            }
        )
        merged["environment"] = environment
    if "marketplaces" in cli_values and cli_values["marketplaces"] is not None:
        merged["marketplaces"] = [
            _normalize_marketplace(str(source), cwd) for source in cli_values["marketplaces"]
        ]
    if "copy_files" in cli_values and cli_values["copy_files"] is not None:
        merged["copy_files"] = [
            str(_resolve_windows_path(str(source), cwd)) for source in cli_values["copy_files"]
        ]
    if cli_values.get("pricing_file") is not None:
        merged["pricing_file"] = str(_resolve_windows_path(str(cli_values["pricing_file"]), cwd))
    if merged.get("prompt_file") is not None:
        if merged.get("prompt") is not None:
            raise ValueError("use either --prompt or --prompt-file, not both")
        prompt_path = _resolve_windows_path(str(merged.pop("prompt_file")), cwd)
        merged["prompt"] = prompt_path.read_text(encoding="utf-8")
    if not merged.get("output"):
        merged["output"] = str(cwd / datetime.now().strftime("test-wsl2-llm-%Y%m%d-%H%M%S"))
    else:
        merged["output"] = str(_resolve_windows_path(str(merged["output"]), cwd))
    return TestConfig.model_validate(merged)


def save_config(config: TestConfig, path: Path) -> None:
    """Write a reusable, fully resolved input configuration."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def output_paths(output: str) -> tuple[Path, Path]:
    """Return same-stem Markdown and YAML output paths."""
    path = Path(output).resolve()
    if path.suffix.lower() in {".md", ".yaml", ".yml"}:
        path = path.with_suffix("")
    return path.with_suffix(".md"), path.with_suffix(".yaml")


def _resolve_windows_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _normalize_marketplace(source: str, base: Path) -> str:
    candidate = Path(source).expanduser()
    if candidate.is_absolute() or (base / candidate).exists():
        return str(_resolve_windows_path(source, base))
    return source
