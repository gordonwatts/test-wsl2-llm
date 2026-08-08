from __future__ import annotations

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("wsl-acceptance")
    group.addoption(
        "--run-wsl-acceptance",
        action="store_true",
        default=False,
        help="run token-consuming tests against real Codex in WSL2",
    )
    group.addoption(
        "--wsl-model",
        default=os.environ.get("TEST_WSL2_LLM_MODEL", "gpt-5.6-luna"),
        help="Codex model for live WSL acceptance tests",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-wsl-acceptance"):
        return
    marker = pytest.mark.skip(reason="requires --run-wsl-acceptance")
    for item in items:
        if "wsl_acceptance" in item.keywords:
            item.add_marker(marker)
