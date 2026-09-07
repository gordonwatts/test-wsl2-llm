"""Compare named numeric output with absolute or relative tolerance."""
import math
import re

from pydantic import BaseModel, ConfigDict, field_validator

from .models import TestResult


class NumCompareArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    var_name: str
    number: float
    tolerance: float | str

    @field_validator("var_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip() or "=" in value or "\n" in value:
            raise ValueError("var_name must be nonempty and cannot contain = or a newline")
        return value

    @field_validator("number")
    @classmethod
    def finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("number must be finite")
        return value

    @field_validator("tolerance")
    @classmethod
    def valid_tolerance(cls, value: float | str) -> float | str:
        if isinstance(value, str):
            if not value.endswith("%"):
                raise ValueError("string tolerance must be a percentage, e.g. 5%")
            amount = float(value[:-1])
        else:
            amount = value
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("tolerance must be finite and nonnegative")
        return value


def num_compare(
    result: TestResult, *, var_name: str, number: float, tolerance: float | str,
) -> tuple[bool, str]:
    from .validation import output_text

    numeric = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    pattern = rf"(?<!\w){re.escape(var_name)}\s*=\s*({numeric})(?![\w.])"
    matches = [float(m.group(1)) for m in re.finditer(pattern, output_text(result))]
    if not matches:
        return False, f"No numeric assignment for {var_name!r} found."
    relative = isinstance(tolerance, str)
    limit = abs(number) * float(tolerance[:-1]) / 100 if relative else tolerance
    # A zero reference permits only exact zero for percentage tolerance.
    passed = any(
        math.isfinite(value) and (
            value == number if limit == 0 else
            abs(value - number) < limit if relative else abs(value - number) <= limit
        ) for value in matches
    )
    return passed, f"{var_name}: observed {matches}; expected {number}, tolerance {tolerance}."
