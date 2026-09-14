from pathlib import Path

from test_wsl2_llm.config import build_config
from test_wsl2_llm.provenance import (
    build_provenance,
    canonical_json,
    effective_configuration_hash,
    hash_directory,
    hash_file,
)


def _config(tmp_path: Path, **overrides):
    values = {
        "prompt": "Do the reproducible thing.",
        "model": "test-model",
        "output": str(tmp_path / "run"),
    }
    values.update(overrides)
    return build_config(values, {})


def test_canonical_hash_is_stable_for_mapping_order(tmp_path: Path) -> None:
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert effective_configuration_hash(_config(tmp_path)) == effective_configuration_hash(
        _config(tmp_path)
    )


def test_local_input_content_changes_identity(tmp_path: Path) -> None:
    input_file = tmp_path / "input.txt"
    input_file.write_text("one", encoding="utf-8")
    first = build_provenance(
        _config(tmp_path, copy_files=[str(input_file)]),
        agent_version=None,
        target="wsl2",
        target_version=None,
    )
    input_file.write_text("two", encoding="utf-8")
    second = build_provenance(
        _config(tmp_path, copy_files=[str(input_file)]),
        agent_version=None,
        target="wsl2",
        target_version=None,
    )
    assert first.identity != second.identity


def test_marketplace_commit_changes_identity_and_keeps_requested_source(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    marketplace.mkdir()
    (marketplace / "plugin.txt").write_text("one", encoding="utf-8")
    config = _config(tmp_path, marketplaces=[str(marketplace)])
    first = build_provenance(
        config, agent_version="codex 1", target="wsl2", target_version=None
    )
    (marketplace / "plugin.txt").write_text("two", encoding="utf-8")
    second = build_provenance(
        config, agent_version="codex 1", target="wsl2", target_version=None
    )
    assert first.identity != second.identity
    assert first.marketplaces[0].requested == str(marketplace)
    assert first.marketplaces[0].resolved == str(marketplace.resolve())


def test_authentication_files_are_not_hashed_or_published(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"token":"secret"}', encoding="utf-8")
    assert hash_file(auth) is None
    marketplace = tmp_path / "marketplace"
    marketplace.mkdir()
    (marketplace / "auth.json").write_text("secret", encoding="utf-8")
    (marketplace / "public.txt").write_text("public", encoding="utf-8")
    digest = hash_directory(marketplace)
    (marketplace / "auth.json").write_text("changed", encoding="utf-8")
    assert digest == hash_directory(marketplace)


def test_unknown_runtime_versions_are_explicit(tmp_path: Path) -> None:
    provenance = build_provenance(
        _config(tmp_path, plugins=["demo@marketplace"]),
        agent_version=None,
        target="wsl2",
        target_version=None,
    )
    assert provenance.agent_version == "unknown"
    assert provenance.target_version == "unknown"
    assert provenance.plugins[0].version == "unknown"
