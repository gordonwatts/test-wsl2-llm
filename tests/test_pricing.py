from pathlib import Path

import yaml

from test_wsl2_llm.models import UsageRecord
from test_wsl2_llm.pricing import load_and_calculate_costs


def test_cost_calculation_separates_cached_and_uncached_input(tmp_path: Path) -> None:
    pricing_file = tmp_path / "pricing.yaml"
    pricing_file.write_text(
        yaml.safe_dump(
            {
                "currency": "USD",
                "models": {
                    "gpt-test": {
                        "input_cost_per_million_tokens": 2,
                        "cached_input_cost_per_million_tokens": 0.5,
                        "output_cost_per_million_tokens": 8,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    usage = [
        UsageRecord(
            model="gpt-test",
            attribution="reported",
            input_tokens=1_000_000,
            cached_input_tokens=200_000,
            output_tokens=100_000,
        )
    ]

    result = load_and_calculate_costs(usage, str(pricing_file))

    cost = result.models[0]
    assert cost.uncached_input_tokens == 800_000
    assert cost.input_cost == 1.6
    assert cost.cached_input_cost == 0.1
    assert cost.output_cost == 0.8
    assert cost.total_cost == 2.5
    assert result.total_cost == 2.5


def test_unknown_model_has_explicitly_unavailable_cost(tmp_path: Path) -> None:
    pricing_file = tmp_path / "pricing.yaml"
    pricing_file.write_text("currency: USD\nmodels: {}\n", encoding="utf-8")
    result = load_and_calculate_costs(
        [UsageRecord(model="unknown", attribution="inferred", input_tokens=10)],
        str(pricing_file),
    )
    assert result.models[0].pricing_available is False
    assert result.models[0].total_cost is None
    assert result.total_cost is None


def test_bundled_catalog_includes_current_claude_models() -> None:
    usage = [
        UsageRecord(
            model=model,
            attribution="reported",
            input_tokens=1_000_000,
            cached_input_tokens=100_000,
            output_tokens=1_000_000,
        )
        for model in (
            "claude-fable-5-1",
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        )
    ]

    result = load_and_calculate_costs(usage, None)

    assert all(model.pricing_available for model in result.models)
    assert [
        (
            model.input_cost_per_million_tokens,
            model.cached_input_cost_per_million_tokens,
            model.output_cost_per_million_tokens,
        )
        for model in result.models
    ] == [
        (10.0, 0.25, 50.0),
        (5.0, 0.5, 25.0),
        (5.0, 0.5, 25.0),
        (2.0, 0.2, 10.0),
        (3.0, 0.3, 15.0),
        (1.0, 0.1, 5.0),
    ]
