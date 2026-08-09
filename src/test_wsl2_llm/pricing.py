"""Load model token prices and calculate usage costs."""

from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import yaml

from test_wsl2_llm.models import ModelCost, ModelInformation, UsageRecord


def load_and_calculate_costs(
    usage: list[UsageRecord], pricing_file: str | None
) -> ModelInformation:
    """Load exact-model pricing and calculate component and aggregate costs."""
    if pricing_file:
        path = Path(pricing_file).resolve()
        return _calculate(usage, _load(path), str(path))

    resource = files("test_wsl2_llm").joinpath("model-pricing.yaml")
    with as_file(resource) as path:
        return _calculate(usage, _load(path), "bundled:model-pricing.yaml")


def _load(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
        raise ValueError(f"pricing YAML must contain a models mapping: {path}")
    return raw


def _calculate(
    usage: list[UsageRecord], catalog: dict[str, Any], source_file: str
) -> ModelInformation:
    currency = str(catalog.get("currency", "USD"))
    configured_models = catalog["models"]
    costs: list[ModelCost] = []
    for record in usage:
        raw = configured_models.get(record.model, {})
        if not isinstance(raw, dict):
            raw = {}
        input_rate = _rate(raw, "input_cost_per_million_tokens")
        cached_rate = _rate(raw, "cached_input_cost_per_million_tokens")
        output_rate = _rate(raw, "output_cost_per_million_tokens")
        available = all(rate is not None for rate in (input_rate, cached_rate, output_rate))
        uncached_tokens = max(0, record.input_tokens - record.cached_input_tokens)
        input_cost = _token_cost(uncached_tokens, input_rate)
        cached_cost = _token_cost(record.cached_input_tokens, cached_rate)
        output_cost = _token_cost(record.output_tokens, output_rate)
        total = (
            input_cost + cached_cost + output_cost
            if available
            and input_cost is not None
            and cached_cost is not None
            and output_cost is not None
            else None
        )
        costs.append(
            ModelCost(
                model=record.model,
                attribution=record.attribution,
                pricing_available=available,
                currency=currency,
                input_cost_per_million_tokens=input_rate,
                cached_input_cost_per_million_tokens=cached_rate,
                output_cost_per_million_tokens=output_rate,
                uncached_input_tokens=uncached_tokens,
                cached_input_tokens=record.cached_input_tokens,
                output_tokens=record.output_tokens,
                input_cost=input_cost,
                cached_input_cost=cached_cost,
                output_cost=output_cost,
                total_cost=total,
                source=_optional_string(raw.get("source")),
                note=_optional_string(raw.get("note")),
            )
        )
    totals = [cost.total_cost for cost in costs]
    aggregate = sum(totals) if totals and all(value is not None for value in totals) else None
    return ModelInformation(
        pricing_file=source_file,
        currency=currency,
        models=costs,
        total_cost=aggregate,
    )


def _rate(value: dict[str, Any], key: str) -> float | None:
    rate = value.get(key)
    if rate is None:
        return None
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate < 0:
        raise ValueError(f"{key} must be a non-negative number or null")
    return float(rate)


def _token_cost(tokens: int, rate: float | None) -> float | None:
    return None if rate is None else tokens * rate / 1_000_000


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
