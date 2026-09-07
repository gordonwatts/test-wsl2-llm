import pytest
from test_report import sample_result

from test_wsl2_llm.models import ValidatorConfig
from test_wsl2_llm.validation import apply_validators, validate_configuration


@pytest.mark.parametrize("text,number,tolerance,passed", [
    ("x=1.1", 1.0, 0.2, True), ("x=1.3", 1.0, 0.2, False),
    ("x = -1.02e2", -100.0, "5%", True), ("x=105", 100.0, "5%", False),
    ("x=0", 0.0, "5%", True), ("x=.1", 0.0, "5%", False),
    ("prefix_x=1", 1.0, 0.0, False), ("x=1oops", 1.0, 0.0, False),
    ("x=1e999", 1.0, 0.1, False), ("x=9; x=1", 1.0, 0.0, True),
])
def test_numeric_comparison(text, number, tolerance, passed):
    result = sample_result()
    result.result.final_message = text
    spec = ValidatorConfig(name="num_compare", arguments={
        "var_name": "x", "number": number, "tolerance": tolerance,
    })
    assert apply_validators(result, [spec]).validation[0].passed is passed


@pytest.mark.parametrize("tolerance", [-1, "-2%", "no", "nan%", float("inf")])
def test_invalid_tolerance(tolerance):
    with pytest.raises(ValueError):
        validate_configuration([ValidatorConfig(name="num_compare", arguments={
            "var_name": "x", "number": 1.0, "tolerance": tolerance,
        })])
