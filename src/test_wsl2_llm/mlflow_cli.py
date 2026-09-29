"""Optional MLflow command group."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

mlflow_app = typer.Typer(
    name="mlflow",
    help="Run registered MLflow prompts over evaluation datasets.",
    no_args_is_help=True,
)


@mlflow_app.command("run")
def mlflow_run(
    prompt: Annotated[str, typer.Argument(help="Registered MLflow prompt name.")],
    dataset: Annotated[str, typer.Argument(help="MLflow evaluation dataset name.")],
    config: Annotated[Path, typer.Argument(help="Template YAML with shared harness settings.")],
    experiment: Annotated[str, typer.Option(help="MLflow experiment name.")] = "test-wsl2-llm",
    question: Annotated[
        list[str] | None,
        typer.Option("--question", help="Dataset record ID or unique name; repeatable."),
    ] = None,
) -> None:
    """Run every selected dataset record through the existing harness."""
    from test_wsl2_llm.mlflow_integration import run_trial

    console = Console(stderr=True)
    try:
        code = run_trial(
            prompt,
            dataset,
            config,
            experiment=experiment,
            questions=question,
            console=console,
        )
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc
    raise typer.Exit(code)
