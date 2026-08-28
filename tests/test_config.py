import logging
from pathlib import Path

import pytest
import yaml

from test_wsl2_llm.config import (
    DEFAULT_CONFIG_ENV,
    build_config,
    load_config_file,
    load_default_config,
    merge_config_values,
    output_paths,
    save_config,
)


def test_cli_overrides_yaml_and_embeds_prompt_file(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("from file", encoding="utf-8")
    config_file = tmp_path / "input.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "prompt_file": "prompt.md",
                "model": "old-model",
                "output": "result",
                "marketplaces": ["marketplace"],
            }
        ),
        encoding="utf-8",
    )
    marketplace = tmp_path / "marketplace"
    marketplace.mkdir()

    loaded = load_config_file(config_file)
    resolved = build_config(loaded, {"model": "new-model"}, cwd=tmp_path)

    assert resolved.prompt == "from file"
    assert resolved.model == "new-model"
    assert resolved.output == str((tmp_path / "result").resolve())
    assert resolved.marketplaces == [str(marketplace.resolve())]


@pytest.mark.parametrize("suffix", ["", ".md", ".yaml", ".yml"])
def test_output_paths_always_have_matching_stems(tmp_path: Path, suffix: str) -> None:
    markdown, result_yaml = output_paths(str(tmp_path / f"trial{suffix}"))
    assert markdown.stem == result_yaml.stem == "trial"
    assert markdown.suffix == ".md"
    assert result_yaml.suffix == ".yaml"


def test_saved_config_is_resolved_and_reusable(tmp_path: Path) -> None:
    resolved = build_config(
        {},
        {"prompt": "hello", "model": "model", "output": str(tmp_path / "out")},
        cwd=tmp_path,
    )
    destination = tmp_path / "saved.yaml"
    save_config(resolved, destination)
    assert yaml.safe_load(destination.read_text(encoding="utf-8"))["prompt"] == "hello"


def test_environment_cli_fields_merge_with_yaml_and_are_saved(tmp_path: Path) -> None:
    resolved = build_config(
        {
            "prompt": "hello",
            "model": "model",
            "output": str(tmp_path / "out"),
            "environment": {
                "unset": ["INCLUDE"],
                "path_remove": [r"C:\Program Files\Microsoft Visual Studio"],
            },
        },
        {"environment": {"unset": ["LIB"], "path_remove": None}},
        cwd=tmp_path,
    )
    destination = tmp_path / "saved.yaml"
    save_config(resolved, destination)

    saved = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert saved["environment"] == {
        "unset": ["LIB"],
        "path_remove": [r"C:\Program Files\Microsoft Visual Studio"],
    }


def test_default_config_is_partial_and_logs_when_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text(
        "environment:\n  unset: [INCLUDE]\n  path_remove: ['C:\\Visual Studio']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(DEFAULT_CONFIG_ENV, str(defaults))

    with caplog.at_level(logging.INFO):
        loaded = load_default_config()

    assert loaded["environment"] == {
        "unset": ["INCLUDE"],
        "path_remove": [r"C:\Visual Studio"],
    }
    assert f"Loading default configuration: {defaults}" in caplog.text


def test_explicit_config_merges_nested_environment_over_defaults() -> None:
    merged = merge_config_values(
        {
            "marketplaces": ["default-marketplace"],
            "environment": {
                "unset": ["INCLUDE"],
                "path_remove": [r"C:\Visual Studio"],
            },
        },
        {"environment": {"unset": ["LIB"]}},
    )

    assert merged == {
        "marketplaces": ["default-marketplace"],
        "environment": {
            "unset": ["LIB"],
            "path_remove": [r"C:\Visual Studio"],
        },
    }


def test_prompt_and_prompt_file_are_mutually_exclusive(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="either"):
        build_config(
            {},
            {
                "prompt": "inline",
                "prompt_file": str(prompt),
                "model": "model",
                "output": str(tmp_path / "out"),
            },
            cwd=tmp_path,
        )


def test_pricing_file_from_yaml_resolves_relative_to_yaml(tmp_path: Path) -> None:
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text("models: {}\n", encoding="utf-8")
    config_file = tmp_path / "input.yaml"
    config_file.write_text(
        "prompt: hello\nmodel: model\noutput: out\npricing_file: pricing.yaml\n",
        encoding="utf-8",
    )
    resolved = build_config(load_config_file(config_file), {}, cwd=tmp_path)
    assert resolved.pricing_file == str(pricing.resolve())


def test_copy_files_from_yaml_resolve_relative_to_yaml(tmp_path: Path) -> None:
    secret = tmp_path / "servicex.yaml"
    secret.write_text("token: secret\n", encoding="utf-8")
    config_file = tmp_path / "input.yaml"
    config_file.write_text(
        "prompt: hello\nmodel: model\noutput: out\ncopy_files: [servicex.yaml]\n",
        encoding="utf-8",
    )

    resolved = build_config(load_config_file(config_file), {}, cwd=tmp_path)

    assert resolved.copy_files == [str(secret.resolve())]


@pytest.mark.parametrize(
    ("model_argument", "expected_model", "expected_effort"),
    [
        ("gpt-test", "gpt-test", "medium"),
        ("gpt-test:minimal", "gpt-test", "minimal"),
        ("gpt-test:low", "gpt-test", "low"),
        ("gpt-test:high", "gpt-test", "high"),
        ("gpt-test:xhigh", "gpt-test", "xhigh"),
    ],
)
def test_model_argument_splits_optional_reasoning_effort(
    tmp_path: Path, model_argument: str, expected_model: str, expected_effort: str
) -> None:
    resolved = build_config(
        {},
        {"prompt": "hello", "model": model_argument, "output": str(tmp_path / "out")},
        cwd=tmp_path,
    )
    assert resolved.model == expected_model
    assert resolved.reasoning_effort == expected_effort


def test_model_argument_rejects_unknown_reasoning_effort(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        build_config(
            {},
            {"prompt": "hello", "model": "gpt-test:extreme", "output": str(tmp_path / "out")},
            cwd=tmp_path,
        )
