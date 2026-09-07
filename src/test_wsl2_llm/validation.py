"""Named, extensible validators operating on the complete collected result."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import TestResult, ValidationResult, ValidatorConfig
from .validation_numeric import NumCompareArguments, num_compare
from .validation_root import RootTreeArguments, root_tree


class RequireStringArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    string: str = Field(min_length=1)


def output_text(result: TestResult) -> str:
    """Current response plus captured stdout and stderr (not configuration/prompts)."""
    return "\n".join(
        [
            result.result.final_message or "",
            result.logs.stdout_jsonl,
            result.logs.stderr,
        ]
    )


def require_string(result: TestResult, *, string: str) -> tuple[bool, str]:
    passed = string in output_text(result)
    return passed, f"Required string {string!r} {'found' if passed else 'not found'} in output."


REGISTRY: dict[str, tuple[type[BaseModel], Callable[..., tuple[bool, str]]]] = {
    "require_string": (RequireStringArguments, require_string),
    "root_tree": (RootTreeArguments, root_tree),
    "num_compare": (NumCompareArguments, num_compare),

}


def register_validator(
    name: str,
    arguments: type[BaseModel],
    validator: Callable[..., tuple[bool, str]],
) -> None:
    """Register an argument schema and a callable receiving TestResult and keyword args."""
    if not name or name in REGISTRY:
        raise ValueError(f"Validator already registered or empty: {name!r}")
    REGISTRY[name] = (arguments, validator)


def validate_configuration(validators: list[ValidatorConfig]) -> None:
    """Reject unknown names and invalid arguments before starting WSL."""
    for specification in validators:
        if specification.name not in REGISTRY:
            known = ", ".join(sorted(REGISTRY)) or "(none registered)"
            raise ValueError(
                f"Unknown validator {specification.name!r}. Known validators: {known}"
            )
        schema, _ = REGISTRY[specification.name]
        schema.model_validate(specification.arguments)


def apply_validators(result: TestResult, validators: list[ValidatorConfig]) -> TestResult:
    """Evaluate every check, retain diagnostics, and fail the run if any check fails."""
    validate_configuration(validators)
    checks = []
    for specification in validators:
        schema, validator = REGISTRY[specification.name]
        arguments: dict[str, Any] = schema.model_validate(specification.arguments).model_dump()
        try:
            passed, message = validator(result, **arguments)
        except Exception as error:
            passed, message = False, f"Validator error: {error}"
        checks.append(
            ValidationResult(
                name=specification.name,
                arguments=arguments,
                passed=passed,
                message=message,
            )
        )
    result.validation = checks
    if any(not check.passed for check in checks):
        result.run.status = "failed"
        result.run.exit_code = result.run.exit_code or 1
        error = "Validation failed: " + ", ".join(c.name for c in checks if not c.passed)
        result.run.error = f"{result.run.error}; {error}" if result.run.error else error
    return result
