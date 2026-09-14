# ruff: noqa: E402

import importlib.util
from pathlib import Path

import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "release_evidence", Path(__file__).parents[1] / "scripts" / "release_evidence.py"
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
REQUIRED_SCENARIOS = _MODULE.REQUIRED_SCENARIOS
build_bundle = _MODULE.build_bundle
check_scenario = _MODULE.check_scenario
sanitize_text = _MODULE.sanitize_text

from test_report import sample_result

from test_wsl2_llm.models import FinalResult, ValidationResult
from test_wsl2_llm.report import write_reports


def _write_scenario(tmp_path: Path, name: str) -> Path:
    result = sample_result()
    if name == "validation-failure":
        result.run.status = "failed"
        result.run.exit_code = 1
        result.validation = [
            ValidationResult(
                name="require_string",
                arguments={"string": "missing"},
                passed=False,
                message="missing",
            )
        ]
    elif name == "timeout":
        result.run.status = "failed"
        result.run.exit_code = 124
        result.run.timed_out = True
        result.result = FinalResult(final_message="partial", timed_out=True)
    elif name == "retained-continuation":
        result.continued_from = "/tmp/private/workspace"
    destination = tmp_path / name
    _, yaml_path = write_reports(result, str(destination))
    return yaml_path


def test_sanitize_text_removes_paths_and_secret_assignments() -> None:
    sanitized = sanitize_text(r"token=abc123 C:\Users\gordon\run /tmp/private/workspace")
    assert "abc123" not in sanitized
    assert "gordon" not in sanitized
    assert "private" not in sanitized
    assert "<redacted>" in sanitized
    assert "<host-path>" in sanitized
    assert "<target-path>" in sanitized


def test_check_scenario_rejects_wrong_status() -> None:
    result = sample_result().model_dump(mode="json")
    with pytest.raises(ValueError, match="success evidence"):
        result["run"]["status"] = "failed"
        check_scenario("success", result)


def test_build_bundle_validates_and_writes_all_evidence(tmp_path: Path) -> None:
    results = {name: _write_scenario(tmp_path, name) for name in REQUIRED_SCENARIOS}
    output = tmp_path / "evidence"
    build_bundle(results, output)

    assert (output / "versions.yaml").exists()
    assert (output / "summary.yaml").exists()
    for name in REQUIRED_SCENARIOS:
        assert (output / "scenarios" / f"{name}.yaml").exists()
        assert (output / "scenarios" / f"{name}.md").exists()
        assert (output / "scenarios" / f"{name}.artifacts.yaml").exists()
    sanitized = yaml.safe_load((output / "scenarios" / "success.yaml").read_text())
    assert sanitized["run"]["workspace_path"] == "<target-path>"
    assert sanitized["command"]["argv"][2] == "TEST_WSL2_LLM_ARG_0=L3RtcA=="
