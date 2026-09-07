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
