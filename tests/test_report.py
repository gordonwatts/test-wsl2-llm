import re
from pathlib import Path

import pytest
import yaml

from test_wsl2_llm.models import (
    CommandResult,
    ConversationTurn,
    CopiedBackFile,
    FinalResult,
    LogsResult,
    ModelCost,
    ModelInformation,
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
from test_wsl2_llm.report import write_markdown, write_reports


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
        model_information=ModelInformation(
            pricing_file="test-pricing.yaml",
            currency="USD",
            models=[
                ModelCost(
                    model="gpt-test",
                    attribution="inferred - model not directly reported",
                    pricing_available=True,
                    currency="USD",
                    input_cost_per_million_tokens=2,
                    cached_input_cost_per_million_tokens=1,
                    output_cost_per_million_tokens=4,
                    uncached_input_tokens=10,
                    output_tokens=2,
                    input_cost=0.00002,
                    cached_input_cost=0,
                    output_cost=0.000008,
                    total_cost=0.000028,
                )
            ],
            total_cost=0.000028,
        ),
        result=FinalResult(final_message="done"),
        workspace=WorkspaceResult(files=[WorkspaceFile(type="file", path="hello.txt", size=6)]),
        command=CommandResult(
            argv=["wsl.exe", "env", "TEST_WSL2_LLM_ARG_0=L3RtcA==", "bash", "-lic"]
        ),
        logs=LogsResult(
            stdout_jsonl='{"type":"turn.completed"}\n',
            stderr="progress\n",
            session_traces=[
                SessionTrace(
                    path="sessions/run.jsonl",
                    content=(
                        '{"type":"event_msg","payload":{"type":"agent_message",'
                        '"message":"Inspecting the saved workspace."}}\n'
                        '{"type":"event","nested":{"value":1}}\n'
                    ),
                )
            ],
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
        "Inspecting the saved workspace.",
        "<summary>Workspace inventory</summary>",
        "file\t6\thello.txt",
        "<summary>Complete Codex stderr</summary>",
        "progress",
        "0h 0m 2s",
        "$0.00",
    ):
        assert expected in markdown
    assert "Complete Codex stdout JSONL" not in markdown
    assert '"nested": {' not in markdown
    assert "<summary>Model activity</summary>" in markdown
    assert "raw session trace remains in YAML" in markdown
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["logs"]["stdout_jsonl"]
    assert markdown.index("<summary>Model activity</summary>") > markdown.index("## Codex command")
    assert markdown.index("<summary>Model activity</summary>") < markdown.index("Schema version")
    assert "2.000000 seconds" not in markdown
    assert "| Run date | " in markdown
    assert re.search(r"\| Started \| \d{2}:\d{2}:\d{2} \|", markdown)
    assert re.search(r"\| Finished \| \d{2}:\d{2}:\d{2} \|", markdown)
    phase_section = markdown.split("## Phase timing", 1)[1].split(
        "## Token usage and cost", 1
    )[0]
    assert "| Phase | Started | Finished | Duration |" in phase_section
    assert re.search(
        r"\| codex_execution \| \d{2}:\d{2}:\d{2} \| \d{2}:\d{2}:\d{2} \| 1s \|",
        phase_section,
    )
    assert markdown.index("## Prompt") < markdown.index("## Final response") < markdown.index(
        "## Skills and marketplaces"
    )
    token_section = markdown.split("## Token usage and cost", 1)[1].split(
        "## Invocation", 1
    )[0]
    assert (
        "| Model | Attribution | Input | Cached input | Output | Reasoning output | USD total |"
        in token_section
    )
    assert "usage` object on each captured `turn.completed` event" in token_section
    assert "`input_tokens` is the total input-token count" in token_section
    assert "`input_tokens - cached_input_tokens`" in token_section
    assert "cached input was served from the prompt cache" in token_section
    assert "uncached-input rate for uncached input" in token_section
    assert "Input rate / 1M" not in token_section
    amounts = re.findall(r"\$\d+\.\d+", token_section)
    assert amounts
    assert all(re.fullmatch(r"\$\d+\.\d{2}", amount) for amount in amounts)
    assert "base64-encoded WSL transport arguments" in markdown
    assert "not credentials or API tokens" in markdown
    assert markdown.index("not credentials or API tokens") < markdown.index(
        "TEST_WSL2_LLM_ARG_0=L3RtcA=="
    )


def test_continuation_report_keeps_new_prompt_at_top_and_history_in_details(tmp_path: Path) -> None:
    result = sample_result()
    result.prompt = "Inspect the existing file."
    result.continued_from = "/tmp/run/workspace"
    result.conversation = [
        ConversationTurn(prompt="Create hello.txt", final_response="done"),
        ConversationTurn(prompt=result.prompt, final_response="inspected"),
    ]
    markdown = write_markdown(
        result, tmp_path / "summary.md", overwrite=True, include_details=True
    ).read_text(encoding="utf-8")
    assert "## Prompt (continuing retained workspace)" in markdown
    assert markdown.index("Inspect the existing file.") < markdown.index("Conversation history")
    assert "Create hello.txt" in markdown
    assert "### Prompt 1" in markdown
    assert "### Final response 1" in markdown
    assert "```text\nCreate hello.txt\n```" in markdown
    history = markdown.split("<summary>Conversation history</summary>", 1)[1].split(
        "</details>", 1
    )[0]
    assert "Inspect the existing file." not in history


def test_prompt_and_final_response_render_as_wrapping_blockquotes(tmp_path: Path) -> None:
    result = sample_result()
    result.prompt = "First prompt line.\nSecond prompt line."
    result.result.final_message = "First response line.\nSecond response line."

    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    assert "> First prompt line.\n> Second prompt line." in markdown
    assert "> First response line.\n> Second response line." in markdown
    assert "```text\nFirst prompt line." not in markdown
    assert "```text\nFirst response line." not in markdown


def test_report_details_include_copied_files_in_configuration(tmp_path: Path) -> None:
    result = sample_result()
    result.configuration["copy_files"] = ["C:/secrets/servicex.yaml"]

    markdown = write_markdown(
        result, tmp_path / "summary.md", overwrite=True, include_details=True
    ).read_text(encoding="utf-8")

    assert "<summary>Resolved configuration</summary>" in markdown
    assert "copy_files:" in markdown
    assert "C:/secrets/servicex.yaml" in markdown


def test_report_renders_copied_back_links_and_text_preview(tmp_path: Path) -> None:
    result = sample_result()
    copied = tmp_path / "run.notes.txt"
    copied.write_text("line 1\nline 2", encoding="utf-8")
    result.copied_back = [
        CopiedBackFile(
            source="notes.txt",
            destination=str(copied),
            type="text",
            size=copied.stat().st_size,
            text_preview="line 1\nline 2",
        )
    ]
    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    assert "## Copied-back files" in markdown
    assert "[run.notes.txt](run.notes.txt)" in markdown
    assert "First 10 lines:" in markdown
    assert "line 1\nline 2" in markdown


def test_report_renders_image_and_root_copied_back_details(tmp_path: Path) -> None:
    result = sample_result()
    image = tmp_path / "run.plot.png"
    image.write_bytes(b"png")
    root = tmp_path / "run.events.root"
    root.write_bytes(b"root")
    result.copied_back = [
        CopiedBackFile(
            source="plot.png",
            destination=str(image),
            type="image",
            size=3,
        ),
        CopiedBackFile(
            source="events.root",
            destination=str(root),
            type="root",
            size=4,
            root_contents=[
                {
                    "path": "events;1",
                    "type": "TTree",
                    "events": 12,
                    "branches": ["pt", "eta"],
                }
            ],
        ),
    ]
    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    assert "[![run.plot.png](data:image/png;base64,cG5n)](run.plot.png)" in markdown
    assert "image_data_uri" not in result.model_dump()["copied_back"][0]
    assert "| `events;1` | TTree | 12 | pt, eta |" in markdown


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


def test_write_markdown_keeps_default_diagnostics_but_omits_raw_details(tmp_path: Path) -> None:
    destination = write_markdown(sample_result(), tmp_path / "summary.md", include_details=False)
    markdown = destination.read_text(encoding="utf-8")
    assert "## Prompt" in markdown
    assert "## Invocation" in markdown
    assert "<summary>Workspace inventory</summary>" in markdown
    assert "file\t6\thello.txt" in markdown
    assert "<summary>Complete Codex stderr</summary>" in markdown
    assert "progress" in markdown
    assert "<summary>Model activity</summary>" in markdown
    assert "Complete Codex stdout JSONL" not in markdown


def test_activity_summary_omits_auto_reviewer_messages(tmp_path: Path) -> None:
    result = sample_result()
    result.logs.session_traces[0].content = (
        '{"type":"event_msg","payload":{"type":"agent_message",'
        '"message":"{\\"risk_level\\":\\"low\\",\\"outcome\\":\\"allow\\"}"}}\n'
        '{"type":"event_msg","payload":{"type":"agent_message",'
        '"message":"Created the requested file."}}\n'
    )
    markdown = write_markdown(result, tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Created the requested file." in markdown
    assert '"risk_level"' not in markdown
