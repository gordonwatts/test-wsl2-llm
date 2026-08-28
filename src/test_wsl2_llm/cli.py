"""Typer command-line interface."""

from __future__ import annotations

import logging
import posixpath
import subprocess
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from test_wsl2_llm.config import (
    build_config,
    load_config_file,
    load_default_config,
    merge_config_values,
    output_paths,
    save_config,
)
from test_wsl2_llm.models import EnvironmentPolicy, TestResult
from test_wsl2_llm.runner import WslClient
from test_wsl2_llm.template import (
    load_template_file,
    render_questions,
    resolved_template_values,
    template_output,
    write_template,
)

app = typer.Typer(
    name="test-wsl2-llm",
    help="Run reproducible Codex CLI tests in fresh WSL2 workspaces.",
    no_args_is_help=True,
)
template_app = typer.Typer(
    name="template",
    help="Create and run prompt templates over a question batch.",
    no_args_is_help=True,
)
app.add_typer(template_app, name="template")
logger = logging.getLogger(__name__)


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
    unset_env: Annotated[
        list[str] | None,
        typer.Option(
            "--unset-env",
            help="Windows environment variable removed before WSL launches; repeatable.",
        ),
    ] = None,
    path_remove: Annotated[
        list[str] | None,
        typer.Option(
            "--path-remove",
            help="Case-insensitive Windows PATH prefix or glob removed before WSL; repeatable.",
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
    repeat: Annotated[
        int,
        typer.Option(
            "--repeat",
            min=1,
            help="Run the test this many times; repeated results use an -001, -002, ... suffix.",
        ),
    ] = 1,
    threads: Annotated[
        int,
        typer.Option(
            "--threads",
            min=1,
            help="Run up to this many repetitions concurrently; defaults to one.",
        ),
    ] = 1,
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
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Maximum Codex execution time in seconds."),
    ] = None,
    max_copy_back: Annotated[
        int | None,
        typer.Option("--max-copy-back", help="Maximum number of files copied back per run."),
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
            "-v",
            "--verbose",
            count=True,
            help=(
                "-v shows commands and removed PATH entries; -vv streams all output and "
                "reports when no PATH entries matched."
            ),
        ),
    ] = 0,
) -> None:
    """Run isolated Codex tests and write paired Markdown/YAML results."""
    console = Console(stderr=True)
    _configure_logging(verbose)
    try:
        file_values = load_default_config()
        if config:
            file_values = merge_config_values(file_values, load_config_file(config))
        cli_values = {
            "prompt": prompt,
            "prompt_file": str(prompt_file) if prompt_file else None,
            "model": model,
            "marketplaces": marketplace,
            "plugins": plugin,
            "copy_files": copy_file,
            "copy_back": copy_back,
            "environment": _environment_cli_values(unset_env, path_remove),
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
            "timeout_seconds": timeout,
            "max_copy_back_files": max_copy_back,
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

        run_outputs = [
            _repeat_output(resolved.output, index, repeat) for index in range(1, repeat + 1)
        ]
        if not resolved.overwrite:
            existing = [
                path
                for run_output in run_outputs
                for path in output_paths(run_output)
                if path.exists()
            ]
            if existing:
                raise FileExistsError(
                    f"result file already exists: {', '.join(map(str, existing))}"
                )

        from test_wsl2_llm.report import write_reports
        from test_wsl2_llm.runner import run_test

        def run_one(
            index: int, run_output: str, repeat_display: _RepeatDisplay | None
        ) -> tuple[int, Path, Path, int]:
            run_config = resolved.model_copy(update={"output": run_output})
            result = run_test(
                run_config,
                verbosity=verbose,
                console=console,
                live_progress=repeat_display is None,
                log_callback=repeat_display.log if repeat_display is not None else None,
                invocation=sys.argv,
            )
            markdown_path, yaml_path = write_reports(result, run_output, resolved.overwrite)
            return index, markdown_path, yaml_path, result.run.exit_code

        exit_codes: list[int] = []
        completed_runs: list[tuple[int, Path, Path, int]] = []

        def collect_runs(repeat_display: _RepeatDisplay | None) -> None:
            with ThreadPoolExecutor(max_workers=min(threads, repeat)) as executor:
                futures = [
                    executor.submit(run_one, index, run_output, repeat_display)
                    for index, run_output in enumerate(run_outputs, start=1)
                ]
                for future in as_completed(futures):
                    completed_runs.append(future.result())
                    if repeat_display is not None:
                        repeat_display.advance()

        if repeat > 1:
            with _RepeatDisplay(console, repeat) as repeat_display:
                collect_runs(repeat_display)
        else:
            collect_runs(None)

        for index, markdown_path, yaml_path, exit_code in sorted(completed_runs):
            if repeat > 1:
                console.print(f"Repeat {index}/{repeat}")
            console.print(f"Markdown result: {markdown_path}")
            console.print(f"YAML result: {yaml_path}")
            if exit_code:
                exit_codes.append(exit_code)
        if exit_codes:
            raise typer.Exit(exit_codes[0])
    except (OSError, ValueError, ValidationError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc


@template_app.command("init")
def template_init(
    output: Annotated[Path, typer.Argument(help="Output YAML filename for the starter template.")],
) -> None:
    """Write a starter template YAML file for editing."""
    console = Console(stderr=True)
    try:
        written = write_template(output)
        console.print(f"Template written: {written}")
    except (OSError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc


@template_app.command("run")
def template_run(
    config: Annotated[
        Path, typer.Argument(help="Input template YAML configuration file.")
    ],
    question_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Optional question IDs to run; omit to run every question."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Codex model as MODEL[:EFFORT]; defaults to the template value."),
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
            "--copy-file", help="Windows file copied into the WSL workspace root; repeatable."
        ),
    ] = None,
    copy_back: Annotated[
        list[str] | None,
        typer.Option(
            "--copy-back", help="Workspace file or glob copied back beside the result; repeatable."
        ),
    ] = None,
    unset_env: Annotated[
        list[str] | None,
        typer.Option(
            "--unset-env",
            help="Windows environment variable removed before WSL launches; repeatable.",
        ),
    ] = None,
    path_remove: Annotated[
        list[str] | None,
        typer.Option(
            "--path-remove",
            help="Case-insensitive Windows PATH prefix or glob removed before WSL; repeatable.",
        ),
    ] = None,
    distro: Annotated[
        str | None, typer.Option(help="WSL distribution; defaults to the template value.")
    ] = None,
    wsl_parent: Annotated[
        str | None, typer.Option(help="Existing WSL parent directory for fresh run roots.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Result stem; each question ID is appended to it.")
    ] = None,
    repeat: Annotated[
        int | None, typer.Option("--repeat", min=1, help="Override the template repetition count.")
    ] = None,
    threads: Annotated[
        int | None,
        typer.Option("--threads", min=1, help="Override the global batch concurrency limit."),
    ] = None,
    question: Annotated[
        list[str] | None,
        typer.Option(
            "--question",
            help="Question ID to run; repeat for multiple selected questions.",
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite existing Markdown/YAML result pairs.")
    ] = False,
    title: Annotated[
        str | None, typer.Option(help="Title written as the first line of each Markdown report.")
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
        Path | None, typer.Option(help="Model token pricing YAML.")
    ] = None,
    progress_lines: Annotated[
        int | None, typer.Option(help="Number of recent progress lines displayed while Codex runs.")
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Maximum Codex execution time in seconds."),
    ] = None,
    max_copy_back: Annotated[
        int | None,
        typer.Option("--max-copy-back", help="Maximum number of files copied back per run."),
    ] = None,
    cleanup: Annotated[
        bool | None,
        typer.Option(
            "--cleanup/--keep-workspace", help="Remove WSL run roots after collection."
        ),
    ] = None,
    save_config_path: Annotated[
        Path | None,
        typer.Option("--save-config", help="Write the resolved reusable template YAML."),
    ] = None,
    config_only: Annotated[
        bool, typer.Option(help="Save configuration without invoking WSL; requires --save-config.")
    ] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "-v",
            "--verbose",
            count=True,
            help=(
                "-v shows commands and removed PATH entries; -vv streams all output and "
                "reports when no PATH entries matched."
            ),
        ),
    ] = 0,
) -> None:
    """Run a prompt template once for every question and repetition."""
    console = Console(stderr=True)
    _configure_logging(verbose)
    try:
        batch, shared, _ = load_template_file(config)
        shared = merge_config_values(load_default_config(), shared)
        if repeat is not None and repeat < 1:
            raise ValueError("repeat must be at least 1")
        if threads is not None and threads < 1:
            raise ValueError("threads must be at least 1")
        effective_repeat = batch.repeat if repeat is None else repeat
        effective_threads = batch.threads if threads is None else threads
        rendered_questions = render_questions(batch)
        requested_questions = [*(question_ids or []), *(question or [])]
        if requested_questions:
            if len(set(requested_questions)) != len(requested_questions):
                raise ValueError("question IDs may not be repeated")
            available = [identifier for identifier, _prompt, _values in rendered_questions]
            available_set = set(available)
            unknown = [
                identifier for identifier in requested_questions if identifier not in available_set
            ]
            if unknown:
                raise ValueError(
                    f"unknown question ID(s): {', '.join(unknown)}; "
                    f"available IDs: {', '.join(available)}"
                )
            selected = set(requested_questions)
            rendered_questions = [
                entry for entry in rendered_questions if entry[0] in selected
            ]
        cli_values = {
            "model": model,
            "marketplaces": marketplace,
            "plugins": plugin,
            "copy_files": copy_file,
            "copy_back": copy_back,
            "environment": _environment_cli_values(unset_env, path_remove),
            "distro": distro,
            "wsl_parent": wsl_parent,
            "output": str(output) if output else None,
            # Template runs are resumable by default. Only an explicit --force
            # opts into rerunning questions with existing result files.
            "overwrite": force,
            "title": title,
            "sandbox": sandbox,
            "network": network,
            "approval_policy": approval_policy,
            "approvals_reviewer": approvals_reviewer,
            "auth_source": auth_source,
            "pricing_file": str(pricing_file) if pricing_file else None,
            "progress_lines": progress_lines,
            "timeout_seconds": timeout,
            "max_copy_back_files": max_copy_back,
            "cleanup": cleanup,
        }
        base_values = dict(shared)
        base_values["prompt"] = rendered_questions[0][1]
        resolved_base = build_config(base_values, cli_values)
        if config_only and not save_config_path:
            raise ValueError("--config-only requires --save-config")
        if save_config_path:
            saved = resolved_template_values(
                batch,
                resolved_base.model_dump(mode="json", exclude={"prompt"}),
                output=resolved_base.output,
                repeat=effective_repeat,
                threads=effective_threads,
            )
            save_config_path = save_config_path.resolve()
            save_config_path.parent.mkdir(parents=True, exist_ok=True)
            save_config_path.write_text(
                yaml.safe_dump(saved, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            console.print(f"Saved template configuration: {save_config_path}")
        if config_only:
            return

        jobs: list[tuple[int, str, int, object]] = []
        for question_index, (identifier, prompt_text, _values) in enumerate(
            rendered_questions, start=1
        ):
            question_jobs: list[tuple[int, str, int, object]] = []
            for repetition in range(1, effective_repeat + 1):
                run_values = dict(shared)
                run_values["prompt"] = prompt_text
                run_values["output"] = resolved_base.output
                run_config = build_config(run_values, cli_values)
                run_config = run_config.model_copy(
                    update={
                        "output": template_output(
                            run_config.output, identifier, repetition, effective_repeat
                        )
                    }
                )
                question_jobs.append((question_index, identifier, repetition, run_config))

            existing = [
                path
                for _question_index, _identifier, _repetition, run_config in question_jobs
                for path in output_paths(run_config.output)
                if path.exists()
            ]
            if existing and not resolved_base.overwrite:
                destination = Path(question_jobs[0][3].output).parent
                logger.warning(
                    "Skipping %s; results exist in %s. Use --force to rerun.",
                    identifier,
                    destination,
                )
                continue
            if existing:
                destination = Path(question_jobs[0][3].output).parent
                logger.info(
                    "Rerunning %s; results exist in %s (--force).",
                    identifier,
                    destination,
                )
            jobs.extend(question_jobs)

        if not jobs:
            console.print("No questions to run.")
            return

        from test_wsl2_llm.report import write_reports
        from test_wsl2_llm.runner import run_test

        def run_one(
            question_index: int,
            identifier: str,
            repetition: int,
            run_config: object,
            repeat_display: _RepeatDisplay | None,
        ) -> tuple[int, str, int, Path, Path, int]:
            result = run_test(
                run_config,
                verbosity=verbose,
                console=console,
                live_progress=repeat_display is None,
                log_callback=repeat_display.log if repeat_display is not None else None,
                invocation=sys.argv,
            )
            markdown_path, yaml_path = write_reports(
                result, run_config.output, resolved_base.overwrite
            )
            return (
                question_index,
                identifier,
                repetition,
                markdown_path,
                yaml_path,
                result.run.exit_code,
            )

        completed_runs: list[tuple[int, str, int, Path, Path, int]] = []

        def collect_runs(repeat_display: _RepeatDisplay | None) -> None:
            with ThreadPoolExecutor(max_workers=min(effective_threads, len(jobs))) as executor:
                futures = [
                    executor.submit(
                        run_one, question_index, identifier, repetition, run_config, repeat_display
                    )
                    for question_index, identifier, repetition, run_config in jobs
                ]
                for future in as_completed(futures):
                    completed_runs.append(future.result())
                    if repeat_display is not None:
                        repeat_display.advance()

        if len(jobs) > 1:
            with _RepeatDisplay(console, len(jobs)) as repeat_display:
                collect_runs(repeat_display)
        else:
            collect_runs(None)

        exit_codes: list[int] = []
        for question_index, identifier, repetition, markdown_path, yaml_path, exit_code in sorted(
            completed_runs
        ):
            del question_index
            console.print(f"Question {identifier} (repeat {repetition}/{effective_repeat})")
            console.print(f"Markdown result: {markdown_path}")
            console.print(f"YAML result: {yaml_path}")
            if exit_code:
                exit_codes.append(exit_code)
        if exit_codes:
            raise typer.Exit(exit_codes[0])
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
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
    access: Annotated[
        Literal["codex", "shell"],
        typer.Option(
            "--access",
            help="Interactive access mode: codex starts Codex; shell starts a Bash shell.",
        ),
    ] = "codex",
    resume: Annotated[
        bool,
        typer.Option("--resume", "-r", help="Resume the most recent conversation in this run."),
    ] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "-v",
            "--verbose",
            count=True,
            help=(
                "-v shows removed PATH entries; -vv reports when no PATH entries matched."
            ),
        ),
    ] = 0,
) -> None:
    """Open an interactive Codex session in the retained run workspace."""
    console = Console(stderr=True)
    _configure_logging(verbose)
    try:
        result = _load_result_yaml(input_yaml)
        workspace = result.run.workspace_path
        if not workspace:
            raise ValueError("the result does not contain a retained workspace path")
        if not result.run.workspace_retained:
            raise ValueError("the result workspace was not retained; rerun without --cleanup")

        policy = EnvironmentPolicy.model_validate(result.configuration.get("environment", {}))
        client = WslClient(result.run.distro, policy)
        if access == "shell" and resume:
            raise ValueError("--resume is only supported with --access codex")
        command = _connect_command(result, resume=resume, access=access, client=client)
        description = "interactive shell" if access == "shell" else "interactive Codex"
        console.print(f"Connecting to {workspace} ({description}; press Ctrl-D to exit)")
        completed = subprocess.run(command, check=False, env=client.environment)
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
    unset_env: Annotated[
        list[str] | None,
        typer.Option(
            "--unset-env",
            help="Windows environment variable removed before WSL launches; repeatable.",
        ),
    ] = None,
    path_remove: Annotated[
        list[str] | None,
        typer.Option(
            "--path-remove",
            help="Case-insensitive Windows PATH prefix or glob removed before WSL; repeatable.",
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
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Maximum Codex execution time in seconds."),
    ] = None,
    max_copy_back: Annotated[
        int | None,
        typer.Option("--max-copy-back", help="Maximum number of files copied back per run."),
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
            "-v",
            "--verbose",
            count=True,
            help=(
                "-v shows commands and removed PATH entries; -vv streams all output and "
                "reports when no PATH entries matched."
            ),
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
        previous_values = {
            key: value for key, value in previous.configuration.items() if key != "continuation_of"
        }
        defaults = merge_config_values(load_default_config(), previous_values)
        defaults = merge_config_values(defaults, file_values)
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
            "environment": _environment_cli_values(unset_env, path_remove),
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
            "timeout_seconds": timeout,
            "max_copy_back_files": max_copy_back,
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


def _connect_command(
    result: TestResult,
    *,
    resume: bool,
    access: Literal["codex", "shell"] = "codex",
    client: WslClient | None = None,
) -> list[str]:
    """Build the interactive WSL command without interpolating report values into a shell."""
    workspace = result.run.workspace_path
    if not workspace:
        raise ValueError("the result does not contain a retained workspace path")
    client = client or WslClient(result.run.distro)
    if access == "shell":
        if resume:
            raise ValueError("--resume is only supported with --access codex")
        script = 'workspace="$1"\ncd -- "$workspace"\nexec bash -li'
        return client.command(
            client.shell_command(script, workspace, interactive_login=True)
        )
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


def _environment_cli_values(
    unset: list[str] | None, path_remove: list[str] | None
) -> dict[str, list[str] | None] | None:
    if unset is None and path_remove is None:
        return None
    return {"unset": unset, "path_remove": path_remove}


def _repeat_output(output: str, index: int, repeat: int) -> str:
    """Return the result stem for one repetition, preserving single-run names."""
    if repeat == 1:
        return output
    path = Path(output)
    if path.suffix.lower() in {".md", ".yaml", ".yml"}:
        path = path.with_suffix("")
    width = max(3, len(str(repeat)))
    return str(path.with_name(f"{path.name}-{index:0{width}d}"))


class _RepeatDisplay:
    """Render the repeat bar and bounded Codex log in one live terminal display."""

    def __init__(self, console: Console, total: int) -> None:
        self._lock = Lock()
        self._recent: deque[str] = deque(maxlen=5)
        self._progress = Progress(
            TextColumn("Repeating runs"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            auto_refresh=False,
        )
        self._task_id = self._progress.add_task("", total=total)
        self._live = Live(
            self._render(), console=console, refresh_per_second=8, transient=True
        )

    def __enter__(self) -> _RepeatDisplay:
        self._live.start(refresh=True)
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._live.stop()

    def log(self, line: str) -> None:
        with self._lock:
            self._recent.append(line)
            self._live.update(self._render())

    def advance(self) -> None:
        with self._lock:
            self._progress.advance(self._task_id)
            self._live.update(self._render())

    def _render(self) -> Group:
        log_text = "\n".join(self._recent) or "Starting Codex..."
        return Group(self._progress, Panel(log_text, title="Codex progress"))


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
