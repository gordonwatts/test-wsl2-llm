from pathlib import Path

import pytest
import yaml

from test_wsl2_llm.models import (
    CommandResult,
    FinalResult,
    LogsResult,
    PhaseTiming,
    RunResult,
    SessionTrace,
    SkillsResult,
    TimingResult,
    TraceEvent,
    UsageRecord,
    WorkspaceFile,
    WorkspaceResult,
)
from test_wsl2_llm.models import TestResult as WslTestResult
from test_wsl2_llm.report import write_reports


def sample_result() -> WslTestResult:
    return WslTestResult(
        prompt="Create hello.txt",
        skills=SkillsResult(
            marketplaces=["C:/marketplace"],
            plugins=["proof@test"],
            directories=["/tmp/run/skills/proof"],
        ),
        run=RunResult(
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:02+00:00",
            total_duration_seconds=2,
            codex_execution_seconds=1.5,
            status="succeeded",
            exit_code=0,
            distro="atlas_al9",
            workspace_path="/tmp/run/workspace",
            workspace_retained=True,
            codex_version="codex-cli 1.0",
        ),
        timing=TimingResult(
            phases=[
                PhaseTiming(
                    name="codex_execution",
                    started_at="2026-01-01T00:00:00+00:00",
                    finished_at="2026-01-01T00:00:01+00:00",
                    duration_seconds=1,
                )
            ],
            trace_events=[TraceEvent(source="stdout_jsonl", sequence=1, event_type="turn.started")],
        ),
        configuration={"model": "gpt-test"},
        usage=[
            UsageRecord(
                model="gpt-test",
                attribution="inferred - model not directly reported",
                input_tokens=10,
                output_tokens=2,
            )
        ],
        result=FinalResult(final_message="done"),
        workspace=WorkspaceResult(files=[WorkspaceFile(type="file", path="hello.txt", size=6)]),
        command=CommandResult(argv=["wsl.exe", "codex", "exec"]),
        logs=LogsResult(
            stdout_jsonl='{"type":"turn.completed"}\n',
            stderr="progress\n",
            session_traces=[SessionTrace(path="sessions/run.jsonl", content="trace\n")],
        ),
    )


def test_paired_reports_share_stem_and_canonical_data(tmp_path: Path) -> None:
    result = sample_result()
    markdown_path, yaml_path = write_reports(result, str(tmp_path / "run"))
    assert markdown_path.stem == yaml_path.stem == "run"
    loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert loaded == result.model_dump(mode="json")
    for expected in (
        result.prompt,
        result.skills.plugins[0],
        result.logs.stdout_jsonl.strip(),
        result.logs.session_traces[0].content.strip(),
        "2.000000 seconds",
    ):
        assert expected in markdown


def test_report_refuses_either_existing_output(tmp_path: Path) -> None:
    (tmp_path / "run.yaml").write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_reports(sample_result(), str(tmp_path / "run"))


def test_report_overwrite_replaces_both(tmp_path: Path) -> None:
    (tmp_path / "run.yaml").write_text("old", encoding="utf-8")
    (tmp_path / "run.md").write_text("old", encoding="utf-8")
    write_reports(sample_result(), str(tmp_path / "run.md"), overwrite=True)
    assert "schema_version" in (tmp_path / "run.yaml").read_text(encoding="utf-8")
    assert "WSL2 Codex test result" in (tmp_path / "run.md").read_text(encoding="utf-8")
