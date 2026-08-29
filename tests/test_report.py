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


def test_skill_directory_report_hides_harness_prefix_but_yaml_keeps_it(tmp_path: Path) -> None:
    result = sample_result()
    full_path = "/tmp/test-wsl2-llm-LXyJJYhz/.harness/codex-home/plugins/cache/example/skills/demo"
    result.skills.directories = [full_path]

    markdown_path, yaml_path = write_reports(result, str(tmp_path / "summary"))
    markdown = markdown_path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    assert "plugins/cache/example/skills/demo" in markdown
    assert "/tmp/test-wsl2-llm-LXyJJYhz/.harness/codex-home" not in markdown
    assert loaded["skills"]["directories"] == [full_path]


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


def test_invocation_renders_as_wrapping_blockquote(tmp_path: Path) -> None:
    result = sample_result()
    result.invocation = "test-wsl2-llm run --output C:/a/very-long-result-path --prompt hello"

    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    invocation = markdown.split("## Invocation", 1)[1].split("## Codex command", 1)[0]
    assert "> test-wsl2-llm run --output C:/a/very-long-result-path --prompt hello" in invocation
    assert "```text" not in invocation


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
    copied.write_text("\n".join(f"line {index}" for index in range(1, 21)), encoding="utf-8")
    result.copied_back = [
        CopiedBackFile(
            source="notes.txt",
            destination=str(copied),
            type="text",
            size=copied.stat().st_size,
            text_preview="\n".join(f"line {index}" for index in range(1, 11)),
        )
    ]
    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    assert "## Copied-back files" in markdown
    assert "[run.notes.txt](run.notes.txt)" in markdown
    assert "Text contents (first 10 lines visible; scroll for the rest):" in markdown
    assert "line 1" in markdown
    assert "line 20" in markdown
    assert 'max-height: 12em; overflow: auto' in markdown
    assert "navigator.clipboard.writeText" in markdown
    assert "📋" in markdown
    assert 'title="Copy to clipboard"' in markdown


def test_report_renders_missing_copy_back_patterns(tmp_path: Path) -> None:
    result = sample_result()
    result.copied_back = []
    result.missing_copy_back = ["plot_*.png", "missing.txt"]
    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    assert "### Missing requested files" in markdown
    assert "`plot_*.png`" in markdown
    assert "`missing.txt`" in markdown


def test_yaml_text_preview_is_literal_text(tmp_path: Path) -> None:
    result = sample_result()
    copied = tmp_path / "run.config.yaml"
    copied.write_text("items:\n  - name: example\n    enabled: true\n", encoding="utf-8")
    result.copied_back = [
        CopiedBackFile(
            source="config.yaml",
            destination=str(copied),
            type="text",
            size=copied.stat().st_size,
            text_preview="items:",
        )
    ]

    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    assert "```text\nitems:" in markdown
    assert "<pre style=" not in markdown.split("config.yaml", 1)[-1].split("</details>", 1)[0]


def test_python_text_preview_marks_code_language(tmp_path: Path) -> None:
    result = sample_result()
    copied = tmp_path / "run.script.py"
    copied.write_text("def greet(name):\n    return f'Hello {name}'\n", encoding="utf-8")
    result.copied_back = [
        CopiedBackFile(
            source="script.py",
            destination=str(copied),
            type="text",
            size=copied.stat().st_size,
            text_preview="def greet(name):\n    return f'Hello {name}'",
        )
    ]

    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    assert "```python\ndef greet(name):" in markdown
    assert '<code class="language-python">' not in markdown
    assert "navigator.clipboard.writeText" in markdown


def test_bash_text_preview_uses_bash_fence(tmp_path: Path) -> None:
    result = sample_result()
    copied = tmp_path / "run.script.sh"
    copied.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$HOME\"\n", encoding="utf-8")
    result.copied_back = [
        CopiedBackFile(
            source="script.sh",
            destination=str(copied),
            type="text",
            size=copied.stat().st_size,
            text_preview="#!/usr/bin/env bash",
        )
    ]

    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    assert "```bash\n#!/usr/bin/env bash" in markdown
    assert 'printf \'%s\\n\' "$HOME"' in markdown
    assert "&quot;" not in markdown


def test_invocation_has_compact_copy_button(tmp_path: Path) -> None:
    result = sample_result()
    result.invocation = "test-wsl2-llm run --prompt hello"

    markdown = write_markdown(result, tmp_path / "summary.md", overwrite=True).read_text(
        encoding="utf-8"
    )
    invocation = markdown.split("## Invocation", 1)[1].split("## Codex command", 1)[0]
    assert "📋" in invocation
    assert 'aria-label="Copy to clipboard"' in invocation
    assert "navigator.clipboard.writeText" in invocation


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


def test_activity_summary_reads_current_stdout_jsonl_events(tmp_path: Path) -> None:
    result = sample_result()
    result.logs.session_traces = []
    result.logs.stdout_jsonl = (
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"Inspecting the repository."}}\n'
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"command":"git status --short","exit_code":0}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    )
    result.timing.trace_events = [
        TraceEvent(source="stdout_jsonl", sequence=1, elapsed_seconds=0.4),
        TraceEvent(source="stdout_jsonl", sequence=2, elapsed_seconds=62.0),
        TraceEvent(source="stdout_jsonl", sequence=3, elapsed_seconds=63.0),
    ]

    markdown = write_markdown(result, tmp_path / "summary.md").read_text(encoding="utf-8")

    assert "| Time | Message |" in markdown
    assert "| 0s | Inspecting the repository. |" in markdown
    assert "| 1m 2s | Command (exit 0): git status --short |" in markdown
    assert "No readable progress updates were recorded." not in markdown
    assert "- done" not in markdown
