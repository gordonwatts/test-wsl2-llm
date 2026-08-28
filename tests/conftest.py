from __future__ import annotations

import os

import pytest

from test_wsl2_llm.config import DEFAULT_CONFIG_ENV


@pytest.fixture(autouse=True)
def isolate_default_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep developer-level defaults from changing deterministic test inputs."""
    monkeypatch.setenv(DEFAULT_CONFIG_ENV, str(tmp_path / "missing-default-config.yaml"))


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
