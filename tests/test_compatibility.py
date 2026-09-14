import json
from pathlib import Path

import pytest
import yaml
from test_report import sample_result

from test_wsl2_llm.compatibility import (
    CONFIG_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    load_result_values,
    normalize_config_values,
    serialize_config,
)
from test_wsl2_llm.config import build_config, save_config
from test_wsl2_llm.models import TestConfig
from test_wsl2_llm.template import TemplateConfig


def test_legacy_config_migrates_and_saved_config_is_versioned(tmp_path: Path) -> None:
    values = normalize_config_values({"prompt": "hello", "model": "gpt-test"})
    config = build_config(values, {"output": str(tmp_path / "result")}, cwd=tmp_path)
    destination = tmp_path / "saved.yaml"
    save_config(config, destination)

    saved = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert saved["schema_version"] == CONFIG_SCHEMA_VERSION
    assert saved["model"] == "gpt-test:medium"


def test_future_config_version_has_actionable_error() -> None:
    with pytest.raises(UnsupportedSchemaVersionError, match="Upgrade test-wsl2-llm"):
        normalize_config_values({"schema_version": CONFIG_SCHEMA_VERSION + 1})


def test_v2_result_loads_with_typed_configuration_snapshot() -> None:
    result = load_result_values(sample_result().model_dump(mode="json"))
    assert result.schema_version == RESULT_SCHEMA_VERSION
    assert result.configuration.model == "gpt-test"
    assert result.configuration.get("mcp_servers") == ["filesystem", "servicex"]


def test_future_result_version_has_actionable_error() -> None:
    values = sample_result().model_dump(mode="json")
    values["schema_version"] = RESULT_SCHEMA_VERSION + 1
    with pytest.raises(UnsupportedSchemaVersionError, match="schema_version=3"):
        load_result_values(values, source="future.yaml")


@pytest.mark.parametrize("selector", ["gpt-test", "gpt-test:high", "gpt-test:xhigh"])
def test_model_selector_round_trips_in_single_run_config(selector: str, tmp_path: Path) -> None:
    config = TestConfig.model_validate(
        {"prompt": "hello", "model": selector, "output": str(tmp_path / "result")}
    )
    serialized = serialize_config(config)
    restored = TestConfig.model_validate(normalize_config_values(serialized))
    assert restored.model_selector == config.model_selector


def test_packaged_editor_schema_matches_template_runtime_fields() -> None:
    schema = json.loads(Path(__file__).parents[1].joinpath("template.schema.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == 1
    example = {
        "prompt_template": "{{ question }}",
        "questions": [{"id": "q1", "question": "hello"}],
    }
    assert TemplateConfig.model_validate(example).questions[0]["id"] == "q1"

