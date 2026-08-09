"""Typer command-line interface."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.logging import RichHandler

from test_wsl2_llm.config import build_config, load_config_file, output_paths, save_config

app = typer.Typer(
    name="test-wsl2-llm",
    help="Run reproducible Codex CLI tests in fresh WSL2 workspaces.",
    no_args_is_help=True,
)


@app.callback()
def application() -> None:
    """Configure and run Codex tests in WSL2."""


@app.command()
def run(
    prompt: Annotated[str | None, typer.Option(help="Prompt text sent to Codex.")] = None,
    prompt_file: Annotated[
        Path | None, typer.Option(help="Windows UTF-8 file containing the prompt.")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Codex model as MODEL[:EFFORT]; effort defaults to medium."),
    ] = None,
    marketplace: Annotated[
        list[str] | None,
        typer.Option(
            "--marketplace",
            help="Windows directory or Git repository URL; cloned into WSL; repeatable.",
        ),
    ] = None,
    plugin: Annotated[
        list[str] | None,
        typer.Option("--plugin", help="Plugin selector such as NAME@MARKETPLACE; repeatable."),
    ] = None,
    distro: Annotated[
        str | None, typer.Option(help="WSL distribution; defaults to WSL default.")
    ] = None,
    wsl_parent: Annotated[
        str | None, typer.Option(help="Existing WSL parent directory for the fresh run root.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Windows result stem or .md/.yaml path; both are written.")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing Markdown/YAML result pair."),
    ] = False,
    title: Annotated[
        str | None, typer.Option(help="Title written as the first line of the Markdown report.")
    ] = None,
    sandbox: Annotated[
        str | None,
        typer.Option(help="Codex sandbox: read-only, workspace-write, or danger-full-access."),
    ] = None,
    network: Annotated[
        bool | None,
        typer.Option("--network/--no-network", help="Allow network in workspace sandbox."),
    ] = None,
    approval_policy: Annotated[
        str | None, typer.Option(help="Codex approval policy: untrusted, on-request, or never.")
    ] = None,
    approvals_reviewer: Annotated[
        str | None, typer.Option(help="Approval reviewer: auto_review or user.")
    ] = None,
    auth_source: Annotated[
        str | None,
        typer.Option(help="Readable WSL Codex auth file copied into isolated CODEX_HOME."),
    ] = None,
    pricing_file: Annotated[
        Path | None,
        typer.Option(help="Model token pricing YAML; defaults to the bundled model-pricing.yaml."),
    ] = None,
    progress_lines: Annotated[
        int | None, typer.Option(help="Number of recent progress lines displayed while Codex runs.")
    ] = None,
    cleanup: Annotated[
        bool | None,
        typer.Option("--cleanup/--keep-workspace", help="Remove WSL run root after collection."),
    ] = None,
    config: Annotated[Path | None, typer.Option(help="Input YAML configuration file.")] = None,
    save_config_path: Annotated[
        Path | None,
        typer.Option("--save-config", help="Write the fully resolved reusable input YAML."),
    ] = None,
    config_only: Annotated[
        bool, typer.Option(help="Save configuration without invoking WSL; requires --save-config.")
    ] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "-v", "--verbose", count=True, help="-v shows commands; -vv streams all output."
        ),
    ] = 0,
) -> None:
    """Run one isolated Codex test and write paired Markdown/YAML results."""
    console = Console(stderr=True)
    _configure_logging(verbose)
    try:
        file_values = load_config_file(config) if config else {}
        cli_values = {
            "prompt": prompt,
            "prompt_file": str(prompt_file) if prompt_file else None,
            "model": model,
            "marketplaces": marketplace,
            "plugins": plugin,
            "distro": distro,
            "wsl_parent": wsl_parent,
            "output": str(output) if output else None,
            "overwrite": True if force else None,
            "title": title,
            "sandbox": sandbox,
            "network": network,
            "approval_policy": approval_policy,
            "approvals_reviewer": approvals_reviewer,
            "auth_source": auth_source,
            "pricing_file": str(pricing_file) if pricing_file else None,
            "progress_lines": progress_lines,
            "cleanup": cleanup,
        }
        resolved = build_config(file_values, cli_values)
        if config_only and not save_config_path:
            raise ValueError("--config-only requires --save-config")
        if save_config_path:
            save_config(resolved, save_config_path)
            console.print(f"Saved configuration: {save_config_path.resolve()}")
        if config_only:
            return

        markdown_path, yaml_path = output_paths(resolved.output)
        if not resolved.overwrite:
            existing = [path for path in (markdown_path, yaml_path) if path.exists()]
            if existing:
                raise FileExistsError(
                    f"result file already exists: {', '.join(map(str, existing))}"
                )

        from test_wsl2_llm.report import write_reports
        from test_wsl2_llm.runner import run_test

        result = run_test(resolved, verbosity=verbose, console=console, invocation=sys.argv)
        markdown_path, yaml_path = write_reports(result, resolved.output, resolved.overwrite)
        console.print(f"Markdown result: {markdown_path}")
        console.print(f"YAML result: {yaml_path}")
        if result.run.exit_code:
            raise typer.Exit(result.run.exit_code)
    except (OSError, ValueError, ValidationError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(show_time=True, show_path=False, markup=False)],
        force=True,
    )


def main() -> None:
    """Console-script entry point."""
    app()
