"""Typer command-line interface."""

from __future__ import annotations

import logging
import posixpath
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.logging import RichHandler

from test_wsl2_llm.config import build_config, load_config_file, output_paths, save_config
from test_wsl2_llm.models import TestResult
from test_wsl2_llm.runner import WslClient

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
    copy_file: Annotated[
        list[str] | None,
        typer.Option(
            "--copy-file",
            help="Windows file copied into the WSL workspace root; repeatable.",
        ),
    ] = None,
    copy_back: Annotated[
        list[str] | None,
        typer.Option(
            "--copy-back",
            help="Workspace file or glob copied back beside the result; repeatable.",
        ),
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
            "copy_files": copy_file,
            "copy_back": copy_back,
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


@app.command("generate")
@app.command("generate-markdown", hidden=True)
def generate_markdown(
    input_yaml: Annotated[Path, typer.Argument(help="YAML result report to render.")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Markdown destination; defaults to the YAML stem."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing Markdown report.")
    ] = False,
    details: Annotated[
        bool,
        typer.Option(
            "--details/--no-details",
            help="Include additional raw logs, traces, configuration, and conversation history.",
        ),
    ] = False,
) -> None:
    """Generate a Markdown report from a previously collected YAML result."""
    console = Console(stderr=True)
    try:
        result = _load_result_yaml(input_yaml)
        destination = output or input_yaml.with_suffix(".md")
        from test_wsl2_llm.report import write_markdown

        written = write_markdown(
            result, destination, overwrite=force, include_details=details
        )
        console.print(f"Markdown result: {written}")
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc


@app.command()
def connect(
    input_yaml: Annotated[Path, typer.Argument(help="YAML result report from a completed run.")],
    resume: Annotated[
        bool,
        typer.Option("--resume", "-r", help="Resume the most recent conversation in this run."),
    ] = False,
) -> None:
    """Open an interactive Codex session in the retained run workspace."""
    console = Console(stderr=True)
    try:
        result = _load_result_yaml(input_yaml)
        workspace = result.run.workspace_path
        if not workspace:
            raise ValueError("the result does not contain a retained workspace path")
        if not result.run.workspace_retained:
            raise ValueError("the result workspace was not retained; rerun without --cleanup")

        command = _connect_command(result, resume=resume)
        console.print(f"Connecting to {workspace} (interactive Codex; press Ctrl-D to exit)")
        completed = subprocess.run(command, check=False)
        if completed.returncode:
            raise typer.Exit(completed.returncode)
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc


@app.command("continue")
def continue_work(
    input_yaml: Annotated[Path, typer.Argument(help="YAML result report from the previous step.")],
    prompt: Annotated[str | None, typer.Option(help="New prompt text sent to Codex.")] = None,
    prompt_file: Annotated[
        Path | None, typer.Option(help="Windows UTF-8 file containing the new prompt.")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Codex model as MODEL[:EFFORT]; defaults to the previous run."),
    ] = None,
    marketplace: Annotated[
        list[str] | None,
        typer.Option(
            "--marketplace",
            help="New Windows marketplace directory or Git URL; repeatable.",
        ),
    ] = None,
    plugin: Annotated[
        list[str] | None,
        typer.Option("--plugin", help="New plugin selector; repeatable."),
    ] = None,
    copy_file: Annotated[
        list[str] | None,
        typer.Option(
            "--copy-file",
            help="New Windows file copied into the retained WSL workspace root; repeatable.",
        ),
    ] = None,
    copy_back: Annotated[
        list[str] | None,
        typer.Option(
            "--copy-back",
            help="Workspace file or glob copied back beside the result; repeatable.",
        ),
    ] = None,
    distro: Annotated[
        str | None, typer.Option(help="WSL distribution; defaults to the previous run.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Output result stem; defaults to <input>-continue.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing Markdown/YAML result pair.")
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
        typer.Option(help="Model token pricing YAML; defaults to the previous run."),
    ] = None,
    progress_lines: Annotated[
        int | None, typer.Option(help="Number of recent progress lines displayed while Codex runs.")
    ] = None,
    config: Annotated[
        Path | None, typer.Option(help="Input YAML overrides for this continuation.")
    ] = None,
    save_config_path: Annotated[
        Path | None,
        typer.Option("--save-config", help="Write the fully resolved continuation YAML."),
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
    """Start a new Codex conversation in the previous run's retained workspace."""
    console = Console(stderr=True)
    _configure_logging(verbose)
    try:
        previous = _load_result_yaml(input_yaml)
        if not previous.run.workspace_path or not previous.run.workspace_retained:
            raise ValueError("the result workspace was not retained; rerun without --cleanup")
        file_values = load_config_file(config) if config else {}
        has_new_prompt = prompt is not None or prompt_file is not None or "prompt" in file_values
        # ``continuation_of`` is report metadata added to continuation results,
        # not an input accepted by ``TestConfig``. Keep it out of the inherited
        # settings so a continuation can itself be continued.
        defaults = {
            key: value for key, value in previous.configuration.items() if key != "continuation_of"
        }
        defaults.update(file_values)
        defaults["prompt"] = prompt or defaults.get("prompt")
        if not defaults.get("model"):
            raise ValueError("the previous result configuration does not contain a model")
        defaults["title"] = defaults.get("title") or previous.title
        defaults["distro"] = defaults.get("distro") or previous.run.distro
        defaults["marketplaces"] = _merge_strings(
            previous.configuration.get("marketplaces") or previous.skills.marketplaces,
            file_values.get("marketplaces", []),
            marketplace or [],
        )
        defaults["plugins"] = _merge_strings(
            previous.configuration.get("plugins") or previous.skills.plugins,
            file_values.get("plugins", []),
            plugin or [],
        )
        defaults["copy_files"] = _merge_strings(
            previous.configuration.get("copy_files", []),
            file_values.get("copy_files", []),
            copy_file or [],
        )
        defaults["copy_back"] = _merge_strings(
            previous.configuration.get("copy_back", []),
            file_values.get("copy_back", []),
            copy_back or [],
        )
        defaults["output"] = str(output or input_yaml.with_name(f"{input_yaml.stem}-continue"))
        defaults["cleanup"] = False
        cli_values = {
            "prompt": prompt,
            "prompt_file": str(prompt_file) if prompt_file else None,
            "model": model,
            "marketplaces": defaults["marketplaces"],
            "plugins": defaults["plugins"],
            "copy_files": defaults["copy_files"],
            "copy_back": defaults["copy_back"],
            "distro": distro,
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
            "cleanup": False,
        }
        resolved = build_config(defaults, cli_values, cwd=input_yaml.parent)
        if not has_new_prompt or not resolved.prompt.strip():
            raise ValueError("continue requires a new --prompt or --prompt-file")
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
        from test_wsl2_llm.runner import continue_test

        result = continue_test(
            previous,
            resolved,
            resolved.prompt,
            verbosity=verbose,
            console=console,
            invocation=sys.argv,
        )
        markdown_path, yaml_path = write_reports(result, resolved.output, resolved.overwrite)
        console.print(f"Markdown result: {markdown_path}")
        console.print(f"YAML result: {yaml_path}")
        if result.run.exit_code:
            raise typer.Exit(result.run.exit_code)
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc


def _connect_command(result: TestResult, *, resume: bool) -> list[str]:
    """Build the interactive WSL command without interpolating report values into a shell."""
    workspace = result.run.workspace_path
    if not workspace:
        raise ValueError("the result does not contain a retained workspace path")
    run_root = posixpath.dirname(workspace)
    codex_home = posixpath.join(run_root, ".harness", "codex-home")
    auth_source = str(result.configuration.get("auth_source") or "~/.codex/auth.json")
    mode = "resume" if resume else "new"
    script = """
set -e
home="$1"
workspace="$2"
auth_source="$3"
mode="$4"
auth_source="${auth_source/#\\~/$HOME}"
mkdir -p -- "$home"
if [ ! -f "$auth_source" ]; then
  printf 'Codex auth file not found: %s\\n' "$auth_source" >&2
  exit 2
fi
cp -- "$auth_source" "$home/auth.json"
chmod 600 "$home/auth.json"
trap 'rm -f -- "$home/auth.json"' EXIT
if [ "$mode" = resume ]; then
  exec env CODEX_HOME="$home" codex resume --last --cd "$workspace"
fi
exec env CODEX_HOME="$home" codex --cd "$workspace"
""".strip()
    client = WslClient(result.run.distro)
    return client.command(
        client.shell_command(
            script, codex_home, workspace, auth_source, mode, interactive_login=True
        )
    )


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(show_time=True, show_path=False, markup=False)],
        force=True,
    )


def _load_result_yaml(path: Path) -> TestResult:
    """Load a result YAML file with an actionable error for the wrong file type."""
    try:
        return TestResult.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as exc:
        raise ValueError(f"while trying to parse file '{path}' as YAML: {exc}") from exc


def _merge_strings(*groups: list[str]) -> list[str]:
    """Combine inherited and newly requested repeatable options in order."""
    return list(dict.fromkeys(value for group in groups for value in group))


def main() -> None:
    """Console-script entry point."""
    app()
