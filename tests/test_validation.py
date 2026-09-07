import pytest
from test_report import sample_result

from test_wsl2_llm.models import ValidatorConfig
from test_wsl2_llm.report import render_markdown
from test_wsl2_llm.validation import REGISTRY, apply_validators, validate_configuration


def check(string):
    return ValidatorConfig(name="require_string", arguments={"string": string})


def test_repeated_checks_and_report_roundtrip():
    result = sample_result()
    result.result.final_message = "Hello world"
    result = apply_validators(result, [check("Hello"), check("missing"), check("world")])
    assert [c.passed for c in result.validation] == [True, False, True]
    assert result.run.status == "failed"
    assert result.run.exit_code != 0
    assert "FAIL" in render_markdown(result)
    assert (
        type(result).model_validate_json(result.model_dump_json()).validation == result.validation
    )


@pytest.mark.parametrize(
    "spec",
    [
        ValidatorConfig(name="unknown"),
        ValidatorConfig(name="require_string"),
        ValidatorConfig(name="require_string", arguments={"string": ""}),
        ValidatorConfig(name="require_string", arguments={"string": 12}),
        ValidatorConfig(name="require_string", arguments={"string": "x", "typo": True}),
    ],
)
def test_invalid_configuration(spec):
    with pytest.raises(ValueError):
        validate_configuration([spec])


def test_unknown_validator_lists_known_names():
    with pytest.raises(ValueError, match=r"Unknown validator 'rquire_string'.*require_string"):
        validate_configuration([ValidatorConfig(name="rquire_string")])


def test_complete_result_access_exception_and_remaining_checks(monkeypatch):
    from test_wsl2_llm.validation import RequireStringArguments

    result = sample_result()

    def broken(received, **kwargs):
        assert received is result
        assert received.logs is result.logs
        assert received.copied_back is result.copied_back
        raise RuntimeError("broken check")

    monkeypatch.setitem(REGISTRY, "broken", (RequireStringArguments, broken))
    result.run.error = "original failure"
    apply_validators(
        result, [ValidatorConfig(name="broken", arguments={"string": "x"}), check("x")]
    )
    assert len(result.validation) == 2
    assert "broken check" in result.validation[0].message
    assert result.run.error.startswith("original failure;")


def test_no_checks_preserves_result():
    result = sample_result()
    before = result.model_dump()
    assert apply_validators(result, []).model_dump() == before


def test_stdout_stderr_and_case_sensitive_search():
    result = sample_result()
    result.logs.stdout_jsonl = "stdout marker"
    result.logs.stderr = "stderr marker"
    apply_validators(result, [check("stdout marker"), check("stderr marker"), check("STDOUT")])
    assert [c.passed for c in result.validation] == [True, True, False]


@pytest.mark.parametrize("continuation", [False, True])
def test_runner_validates_complete_collected_result(monkeypatch, continuation):
    from unittest.mock import MagicMock

    from test_wsl2_llm import runner
    from test_wsl2_llm.models import CopiedBackFile
    from test_wsl2_llm.models import TestConfig as RunConfig
    from test_wsl2_llm.validation import RequireStringArguments

    client = MagicMock()
    client.text.return_value = "/tmp/run"
    client.command.return_value = ["mock-codex"]
    monkeypatch.setattr(runner, "WslClient", lambda *a: client)
    for name in ["_write_wsl_file", "_transfer_files"]:
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    for name in ["_transfer_marketplaces", "_skill_directories", "_inventory"]:
        monkeypatch.setattr(runner, name, lambda *a, **k: [])
    monkeypatch.setattr(runner, "_resolve_wsl_path", lambda *a, **k: "/tmp/mock")
    monkeypatch.setattr(runner, "_stream_codex", lambda *a, **k: (0, "captured", "", [], []))
    monkeypatch.setattr(runner, "_session_traces", lambda *a: ([], []))
    copied = CopiedBackFile(source="a.txt", destination="local/a.txt", type="file", size=1)
    monkeypatch.setattr(runner, "_copy_back_files", lambda *a, **k: [copied])
    seen = []

    def inspect_result(result, **kwargs):
        seen.append(result)
        assert result.copied_back == [copied]
        assert result.logs.stdout_jsonl == "captured"
        return False, "collection inspected"

    monkeypatch.setitem(REGISTRY, "inspect", (RequireStringArguments, inspect_result))
    config = RunConfig(
        prompt="p",
        model="gpt-5",
        output="unused",
        validators=[
            ValidatorConfig(name="inspect", arguments={"string": "x"}),
        ],
    )
    previous = sample_result()
    previous.run.workspace_retained = True
    previous.run.workspace_path = "/tmp/run/workspace"
    result = (
        runner.continue_test(previous, config, "next")
        if continuation
        else runner.run_test(config, live_progress=False)
    )
    assert seen == [result]
    assert result.validation[0].message == "collection inspected"
    assert result.run.status == "failed"
    assert result.run.exit_code == 1


@pytest.mark.parametrize("continuation", [False, True])
def test_unknown_validator_rejected_before_wsl(monkeypatch, continuation):
    from test_wsl2_llm import runner
    from test_wsl2_llm.models import TestConfig as RunConfig

    def forbidden(*args):
        pytest.fail("WSL must not be constructed for invalid validators")

    monkeypatch.setattr(runner, "WslClient", forbidden)
    config = RunConfig(
        prompt="p",
        model="gpt-5",
        output="unused",
        validators=[
            ValidatorConfig(name="unknown"),
        ],
    )
    previous = sample_result()
    previous.run.workspace_retained = True
    previous.run.workspace_path = "/tmp/run/workspace"
    with pytest.raises(ValueError, match="Unknown validator"):
        if continuation:
            runner.continue_test(previous, config, "next")
        else:
            runner.run_test(config)
