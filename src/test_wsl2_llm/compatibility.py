"""Versioned loading and serialization boundaries for saved harness data."""

from collections.abc import Mapping
from typing import Any

import yaml

from .models import ConfigurationSnapshot, TestConfig, TestResult

CONFIG_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 2


class UnsupportedSchemaVersionError(ValueError):
    """Raised when a saved document was produced by an unknown future schema."""


def _version_error(
    kind: str, version: Any, supported: int, source: str | None
) -> UnsupportedSchemaVersionError:
    location = f" in '{source}'" if source else ""
    return UnsupportedSchemaVersionError(
        f"unsupported {kind} schema_version={version!r}{location}; "
        f"this version supports schema_version={supported}. Upgrade test-wsl2-llm "
        "or convert the file with a compatible release."
    )


def normalize_config_values(
    values: Mapping[str, Any], *, source: str | None = None
) -> dict[str, Any]:
    """Validate a configuration document version and return runtime values.

    Unversioned files are the pre-1.0 format and are deliberately accepted as
    a one-way migration. They are never written back without a version marker.
    """
    if not isinstance(values, Mapping):
        raise ValueError("configuration YAML must contain a mapping")
    version = values.get("schema_version", 0)
    if version not in (0, CONFIG_SCHEMA_VERSION):
        raise _version_error("configuration", version, CONFIG_SCHEMA_VERSION, source)
    normalized = dict(values)
    normalized.pop("schema_version", None)
    return normalized


def serialize_config(config: TestConfig) -> dict[str, Any]:
    """Serialize a resolved config using the stable v1 wire representation."""
    values = config.model_dump(mode="json")
    values["schema_version"] = CONFIG_SCHEMA_VERSION
    return values


def load_result_values(values: Mapping[str, Any], *, source: str | None = None) -> TestResult:
    """Validate one result document at the single result-loading boundary."""
    if not isinstance(values, Mapping):
        raise ValueError("result YAML must contain a mapping")
    version = values.get("schema_version")
    if version != RESULT_SCHEMA_VERSION:
        raise _version_error("result", version, RESULT_SCHEMA_VERSION, source)
    try:
        return TestResult.model_validate(dict(values))
    except Exception as exc:
        raise ValueError(f"invalid result schema in '{source or '<input>'}': {exc}") from exc


def serialize_result(result: TestResult) -> dict[str, Any]:
    """Serialize a result without allowing model internals to leak into YAML."""
    return result.model_dump(mode="json")


def load_result_yaml(path) -> TestResult:
    """Read and validate a result YAML file with actionable diagnostics."""
    try:
        values = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"while trying to parse file '{path}' as YAML: {exc}") from exc
    return load_result_values(values, source=str(path))


def configuration_snapshot(config: TestConfig | Mapping[str, Any]) -> ConfigurationSnapshot:
    """Build the typed result configuration snapshot."""
    values = serialize_config(config) if isinstance(config, TestConfig) else dict(config)
    values.setdefault("schema_version", CONFIG_SCHEMA_VERSION)
    return ConfigurationSnapshot.model_validate(values)
