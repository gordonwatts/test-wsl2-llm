"""Deterministic, non-secret identities for test inputs and runtimes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from test_wsl2_llm import __version__
from test_wsl2_llm.models import (
    InputProvenance,
    MarketplaceProvenance,
    PluginProvenance,
    Provenance,
    TestConfig,
)

UNKNOWN_VERSION = "unknown"
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "auth_source",
    "authorization",
    "credential",
    "credentials",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_SENSITIVE_PATH_PARTS = {
    "auth.json",
    "credentials.json",
    "config.toml",
}
_SENSITIVE_PATH_MARKERS = ("auth", "credential", "password", "secret", "token")


def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible data with stable key and whitespace ordering."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(value: Any) -> str:
    """Return a SHA-256 hash of the canonical JSON representation of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_hash(content: bytes) -> str:
    """Return the stable SHA-256 hash used for input content."""
    return hashlib.sha256(content).hexdigest()


def _public_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return None
    if isinstance(value, dict):
        return {
            str(name): cleaned
            for name, child in sorted(value.items(), key=lambda item: str(item[0]))
            if (cleaned := _public_value(child, key=str(name))) is not None
        }
    if isinstance(value, list):
        return [_public_value(child) for child in value]
    if isinstance(value, tuple):
        return [_public_value(child) for child in value]
    return value


def public_configuration(config: TestConfig | dict[str, Any]) -> dict[str, Any]:
    """Return configuration data safe to use in a public identity or report."""
    values = config.model_dump(mode="json") if isinstance(config, TestConfig) else dict(config)
    cleaned = _public_value(values)
    assert isinstance(cleaned, dict)
    for name in ("output", "overwrite"):
        cleaned.pop(name, None)
    return cleaned


def effective_configuration_hash(config: TestConfig | dict[str, Any]) -> str:
    """Hash behavior-affecting configuration while excluding destinations and secrets."""
    return stable_hash(public_configuration(config))


def is_sensitive_path(value: str | Path) -> bool:
    """Recognize common authentication/credential files without reading them."""
    path = Path(value)
    parts = {part.casefold() for part in path.parts}
    if parts & _SENSITIVE_PATH_PARTS:
        return True
    name = path.name.casefold()
    return any(marker in name for marker in _SENSITIVE_PATH_MARKERS)


def hash_file(path: str | Path) -> str | None:
    """Hash a non-sensitive file, returning ``None`` for missing or secret files."""
    path = Path(path)
    if is_sensitive_path(path) or not path.is_file():
        return None
    try:
        return content_hash(path.read_bytes())
    except OSError:
        return None


def hash_directory(path: str | Path) -> str | None:
    """Hash a local directory in canonical relative-path order.

    Git metadata and files whose names indicate credentials are intentionally excluded.
    This keeps a local marketplace identity reproducible without hashing authentication
    files stored alongside a marketplace checkout.
    """
    root = Path(path)
    if not root.is_dir():
        return None
    entries: list[tuple[str, str, bytes]] = []
    try:
        for child in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            relative = child.relative_to(root).as_posix()
            if ".git" in Path(relative).parts or is_sensitive_path(relative):
                continue
            if child.is_symlink():
                entries.append((relative, "symlink", os.readlink(child).encode("utf-8")))
            elif child.is_file():
                entries.append((relative, "file", child.read_bytes()))
    except OSError:
        return None
    digest = hashlib.sha256()
    for relative, kind, data in entries:
        digest.update(kind.encode("ascii"))
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _source_parts(source: str) -> tuple[str, str | None]:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "ssh", "git"} and parsed.netloc:
        path, separator, branch = parsed.path.rpartition("@")
        if separator and path and branch:
            return parsed._replace(path=path).geturl(), branch
    repository, separator, branch = source.rpartition("@")
    if separator and branch and source.find(":") < source.rfind("@"):
        return repository, branch
    return source, None


def input_provenance(config: TestConfig) -> list[InputProvenance]:
    """Describe hashable prompt and copied-in inputs in canonical order."""
    values = [
        InputProvenance(
            kind="prompt",
            requested="prompt",
            content_hash=content_hash(config.prompt.encode("utf-8")),
        )
    ]
    for source in sorted(config.copy_files, key=lambda item: str(item).casefold()):
        path = Path(source)
        if is_sensitive_path(path):
            continue
        values.append(
            InputProvenance(
                kind="copy_file",
                requested=str(source),
                resolved=str(path.resolve()) if path.exists() else None,
                content_hash=hash_file(path),
            )
        )
    return values


def marketplace_provenance(
    sources: list[str],
    *,
    resolved_versions: dict[str, str] | None = None,
) -> list[MarketplaceProvenance]:
    """Describe requested marketplace sources and their resolved versions."""
    versions = resolved_versions or {}
    values: list[MarketplaceProvenance] = []
    for source in sources:
        path = Path(source)
        if path.exists():
            values.append(
                MarketplaceProvenance(
                    requested=source,
                    resolved=str(path.resolve()),
                    version=UNKNOWN_VERSION,
                    content_hash=hash_directory(path),
                )
            )
            continue
        repository, branch = _source_parts(source)
        values.append(
            MarketplaceProvenance(
                requested=source,
                resolved=repository,
                version=versions.get(source, UNKNOWN_VERSION),
                selector=branch,
            )
        )
    return sorted(values, key=lambda item: (item.requested.casefold(), item.requested))


def plugin_provenance(
    selectors: list[str], versions: dict[str, str] | None = None
) -> list[PluginProvenance]:
    """Describe plugin selectors and installed versions, using ``unknown`` explicitly."""
    resolved = versions or {}
    return [
        PluginProvenance(
            requested=selector,
            resolved=selector.split("@", 1)[0],
            version=resolved.get(selector, UNKNOWN_VERSION),
        )
        for selector in sorted(set(selectors), key=lambda item: (item.casefold(), item))
    ]


def build_provenance(
    config: TestConfig,
    *,
    agent_version: str | None,
    target: str,
    target_version: str | None,
    marketplace_versions: dict[str, str] | None = None,
    plugin_versions: dict[str, str] | None = None,
) -> Provenance:
    """Build the public run provenance and a complete effective identity."""
    inputs = input_provenance(config)
    marketplaces = marketplace_provenance(
        config.marketplaces, resolved_versions=marketplace_versions
    )
    plugins = plugin_provenance(config.plugins, plugin_versions)
    identity_values = {
        "agent": config.agent,
        "agent_version": agent_version or UNKNOWN_VERSION,
        "configuration_hash": effective_configuration_hash(config),
        "harness_version": __version__,
        "inputs": [item.model_dump(mode="json") for item in inputs],
        "marketplaces": [item.model_dump(mode="json") for item in marketplaces],
        "plugins": [item.model_dump(mode="json") for item in plugins],
        "target": target,
        "target_version": target_version or UNKNOWN_VERSION,
    }
    return Provenance(
        harness_version=__version__,
        agent=config.agent,
        agent_version=agent_version or UNKNOWN_VERSION,
        target=target,
        target_version=target_version or UNKNOWN_VERSION,
        configuration_hash=identity_values["configuration_hash"],
        inputs=inputs,
        marketplaces=marketplaces,
        plugins=plugins,
        identity=stable_hash(identity_values),
    )
