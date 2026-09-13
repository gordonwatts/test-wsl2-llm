# test-wsl2-llm

`test-wsl2-llm` runs the Codex CLI from Windows inside a fresh WSL2 workspace. It copies Windows-side prompts and local plugin marketplaces into WSL, isolates Codex configuration while reusing a protected copy of existing authentication, and writes matching Markdown and YAML results.

## Supported environments

The support boundary is intentionally explicit:

| Agent | Execution target | Status |
| --- | --- | --- |
| Codex CLI (non-interactive `codex exec`) | Windows host with a WSL2 distribution | Supported |
| Codex CLI (`connect`/`continue`) | Retained workspace in the same WSL2 distribution | Supported |
| Claude Code | Any target | Not implemented in this package |
| Codex or Claude Code | Native Linux, macOS, or passwordless SSH | Not supported yet |

The package name and `test-wsl2-llm` command are stable. The target and agent
rows above describe the current release boundary; they are not promises about
the future target work tracked in the project.

## Clean-install quickstart

Install directly from the GitHub repository in a directory that is not a source
checkout. PyPI publication is not part of the current distribution plan:

```powershell
uv tool install git+https://github.com/gordonwatts/test-wsl2-llm.git
test-wsl2-llm --help
```

Pin the install to a reviewed tag or commit when reproducibility matters:

```powershell
uv tool install git+https://github.com/gordonwatts/test-wsl2-llm.git@v0.1.0
test-wsl2-llm --help
```

For a one-shot invocation without installing a persistent tool, use `uvx`:

```powershell
uvx --from git+https://github.com/gordonwatts/test-wsl2-llm.git test-wsl2-llm --help
```

The wheel and source distribution built by CI are validation artifacts for
clean-install checks; they are not currently uploaded to PyPI. A locally built
wheel can still be installed with `python -m pip install PATH\\TO\\wheel.whl`
when inspecting an artifact.

To verify an installed wheel without relying on the source tree, run this
from a fresh directory:

```powershell
python -c "from importlib.resources import files; p=files('test_wsl2_llm'); assert (p/'template.schema.json').is_file(); assert (p/'model-pricing.yaml').is_file()"
test-wsl2-llm template init .\questions.yaml
```

### Prerequisites and authentication

The supported target needs Windows WSL2, a running Linux distribution with
`bash`, and the Codex CLI on that distribution's login-shell `PATH`:

```powershell
wsl --install
wsl -d <distribution> -- bash -lic "codex --version"
```

Log Codex into that distribution before running a test. By default the
harness reads the WSL file `~/.codex/auth.json`, copies it into a temporary
isolated Codex home with restrictive permissions, and removes the copy during
cleanup. Use `--auth-source PATH` (or `auth_source` in YAML) when the readable
WSL auth file is elsewhere. Authentication files and their contents are never
written to reports. A real Codex account and model access are required for a
live run; normal CI tests do not make model calls.

### One short validated run

Create a YAML file outside the repository so the run validates both the model
response and a returned artifact:

```yaml
prompt: |
  Create hello.txt in the workspace containing exactly "Hello from WSL".
  In your final response include the word READY.
model: MODEL:medium
copy_back:
  - hello.txt
validators:
  - name: require_string
    arguments:
      string: READY
output: .\results\hello
```

Run it from PowerShell:

```powershell
test-wsl2-llm run --config .\quickstart.yaml
```

Success means `results\hello.yaml` records a passed `require_string` check,
`results\hello.md` is readable, and `results\hello.output.hello.txt` contains
the requested file. Add `distro: <distribution>` when the WSL default is not
the distribution where Codex is installed.

### Templates and retained workspaces

`template init questions.yaml` creates a starter batch. `template run` skips
matching valid result pairs on a later invocation; use `--force` to rerun all
selected cells or `--retry failed`/`--retry incomplete` for targeted recovery.
For interactive inspection, run with `--keep-workspace` (equivalently,
`cleanup: false` in YAML), then use `connect result.yaml --resume` or
`continue result.yaml --prompt "Review the result." --output results\review`.
Without retention, the temporary WSL workspace is intentionally removed after
the report and copied-back artifacts are saved.

### When setup fails

The first setup check is intentionally before workspace creation. Use the
error's nearest matching remedy:

| Error or symptom | Setup to check |
| --- | --- |
| `wsl.exe` is not found or the WSL command cannot start | Install/enable WSL2 and confirm `wsl -l -v` lists the chosen distribution. |
| `codex --version` fails or Codex exits before a response | Install Codex in the WSL distribution's login shell and confirm model/account access with `wsl -d <distribution> -- bash -lic "codex --version"`. |
| `Codex auth file not found` | Log in inside that WSL distribution or pass the correct readable WSL path with `--auth-source`. |
| Marketplace/plugin installation fails | Check the Git URL, optional `@branch`, and `plugin-name@marketplace-name` selector; run without those options to isolate the base setup. |
| `connect`/`continue` says the workspace was not retained | Rerun with `--keep-workspace` or `cleanup: false`; a cleaned run cannot be reopened. |

See [`RELEASING.md`](RELEASING.md) for the version, tag, changelog, artifact,
GitHub-install, and publication checklist. It does not publish a release.

## Install and Run

Python 3.11 or newer is required. Install the package with the [uv tool](https://docs.astral.sh/uv/),
which creates or uses an appropriate Python environment automatically:

```powershell
uvx --from git+https://github.com/gordonwatts/test-wsl2-llm.git test-wsl2-llm run `
  --distro atlas_al9 `
  --model MODEL[:EFFORT] `
  --copy-file .\servicex.yaml `
  --copy-back output.png `
  --prompt "Create hello.txt containing Hello from WSL" `
  --output .\results\hello
```

This writes `results\hello.md` for people and `results\hello.yaml` for code. The Markdown report contains the prompt, final response, selected marketplaces, plugins, and MCP servers, concise model-activity updates, timing, token usage, workspace inventory, and complete Codex stderr output. The YAML report retains the raw Codex JSONL and collected session traces for debugging.

Use `--repeat N` to run the same test more than once. For repeated runs, the Markdown,
YAML, and any `--copy-back` artifacts are indexed with a three-digit suffix, starting at
`-001` (for example, `results\hello-001.md`, `results\hello-001.yaml`, and
`results\hello-001.output.png`). The default `--repeat 1` keeps the unsuffixed output
name. Existing indexed results are checked before the first run; use `--force` to replace
them.

Use `--threads N` with `--repeat` to run up to `N` fresh WSL2 tests concurrently. For
example, `--repeat 10 --threads 4` runs ten repetitions in batches of at most four. The
default is `--threads 1`, and the number of workers is capped at the repeat count. Each
repetition still has its own indexed reports, workspace, logs, and copied-back artifacts.
While repetitions are running, a transient aggregate progress bar shows completed runs;
it is removed before the indexed output paths are printed. Repeated-run Codex progress is
written as ordinary log lines so it does not compete with the aggregate live display.
Single runs keep the live Codex progress panel and do not show the aggregate bar.
Status updates are condensed to one line and truncated when an event contains a long
command or message.

Press Ctrl-C during a repeated or template batch to cancel queued jobs before they start
and stop active Codex processes within a bounded grace period. Started jobs keep their
partial logs and reports; the console identifies jobs that were not started. Any worker
or report-write failures are reported together so completed result paths remain visible.

Each run has a 30-minute Codex execution timeout by default. Use `--timeout SECONDS`
to choose a different limit. A timed-out run is stopped, its partial logs and workspace
inventory are still collected, and the report is marked failed. Pressing Ctrl-C has the
same cleanup behavior and records an interruption in the report instead of losing the
partial result.

## Template batches

Create a starter batch file, then edit its prompt and questions:

```powershell
test-wsl2-llm template init .\questions.yaml
```

Run the template with one isolated WSL2 job per model, question, and repetition:

```powershell
test-wsl2-llm template run .\questions.yaml
```

Pass one or more question IDs after the config to run only those questions:

```powershell
test-wsl2-llm template run .\questions.yaml q1 q3
```

The equivalent repeatable flag form is also supported:

```powershell
test-wsl2-llm template run .\questions.yaml --question q1 --question q3
```

If no IDs are supplied, every question is run. Unknown or duplicate IDs are
rejected before any WSL job starts.

Template runs are resumable by default. Each saved YAML report contains the
question/model/repetition identity and a fingerprint of the effective prompt and
run settings. A cell is skipped only when both reports exist, the YAML is a
readable canonical result, and that identity and fingerprint match. Missing,
partial, corrupt, or stale reports are reported and skipped. A matching
successful result is skipped; matching failed results remain skipped to preserve
resume behavior, and can be retried explicitly with `--retry failed`. Use
`--retry incomplete` to retry saved reports that are missing a pair, invalid, or
missing cell metadata. Stale reports are regenerated because their effective
prompt or settings no longer match. Supplying `--force` reruns all selected cells
and overwrites their reports.
Repeat `--model` to compare model/effort combinations (this replaces the YAML
model selection):

```powershell
test-wsl2-llm template run .\questions.yaml --model gpt-5.4:high --model gpt-5.4:low
```

Alternatively, use a YAML list:

```yaml
models:
  - gpt-5.4:high
  - gpt-5.4:low
```

The existing scalar `model: MODEL:EFFORT` and single `--model MODEL:EFFORT`
remain supported. `models` takes precedence over a scalar `model` when both are
present. An omitted effort defaults to `medium`. Empty or
duplicate model selections are rejected before execution. `--save-config`
preserves the effective model list for subsequent runs.

The YAML uses a shared `prompt_template` and a list of question mappings. Every
mapping needs a unique, filename-safe `id`; its scalar fields are available through strict
`{{ field }}` substitutions. A question can reuse the complete `question` text from another
mapping with `{{question-id}}` (including IDs with periods or hyphens). References may be
nested, but cycles and references to a question without text are rejected before execution.
Question-level `copy_back` and `plugins` fields are lists of files/wildcards and plugin
selectors, respectively, not substitution scalars. A question-level `validators` list replaces the
shared checks for that question; omit it to inherit shared checks or use `[]` to disable them.
A question-level `distro` selects a different WSL distribution. For example:

```yaml
prompt_template: |
  Plot {{ quantity }} for the dataset {{ dataset }}.
  Save plots as plot_<n>.png and put the code in script.py.
questions:
  - id: etmiss
    quantity: ETmiss
    dataset: user.example:dataset_a
  - id: shared-selection
    question: Select jets with pT > 30 GeV.
  - id: jet-selection
    quantity: selected jets
    dataset: user.example:dataset_a
    question: |
      {{shared-selection}}
      Apply this selection before plotting.
    copy_back:
      - etmiss.root
      - -ab-output.root
    plugins:
      - etmiss-tools@my-marketplace
      - -shared-tools@my-marketplace
  - id: leading-jet-pt
    quantity: leading-jet pT
    dataset: user.example:dataset_b
model: MODEL:high
marketplaces:
  - https://github.com/example/my-marketplace.git
plugins:
  - shared-tools@my-marketplace
copy_files:
  - .\servicex.yaml
copy_back:
  - plot_*.png
  - script.py
output: .\results\analysis
repeat: 2
threads: 4
```

### Template YAML specification and VS Code support

[`template.schema.json`](template.schema.json) is the canonical machine-readable JSON Schema for
template files. It describes the batch fields, the single-run settings that can be
shared by every job, question value types, and the built-in validators. The schema is
also useful as a quick reference: in a question, `id`, `copy_back`, `plugins`, `distro`, and `validators` are reserved;
every other key must be an identifier and its value must be a scalar string, number, or
boolean. Those scalar keys are the only values that can be substituted in
`prompt_template` with `{{ field }}`. A question `id` must be unique, and every
placeholder must have a value; these cross-field checks remain runtime checks because
JSON Schema cannot express them for arbitrary question keys.

The top-level template fields are:

| Field | Meaning |
| --- | --- |
| `prompt_template` | Required shared prompt. Use strict `{{ field }}` placeholders. |
| `questions` | Required non-empty list of mappings with a unique filename-safe `id`. |
| `model` / `models` | One selector or a non-empty list of `MODEL[:EFFORT]` selectors. `models` wins when both are present; CLI `--model` wins over the YAML selection. |
| `repeat` / `threads` | Positive job repetition count and global concurrency limit; both default to `1`. |
| `title`, `marketplaces`, `plugins`, `mcp_servers` | Report heading and optional Codex marketplace, plugin, and named MCP-server settings. |
| `copy_files`, `copy_back`, `max_copy_back_files` | Files copied into the WSL workspace, files/globs copied back, and the per-job copy-back limit. |
| `validators` | Post-run `require_string`, `num_compare`, or `root_tree` checks. |
| `environment` | `unset` and `path_remove` lists for filtering the inherited Windows environment. |
| `distro`, `wsl_parent`, `output` | WSL distribution, temporary-run parent, and result stem. |
| `sandbox`, `network`, `approval_policy`, `approvals_reviewer` | Codex execution and approval policies. |
| `auth_source`, `pricing_file`, `progress_lines`, `timeout_seconds`, `cleanup`, `overwrite` | Authentication, pricing, progress, timeout, workspace lifetime, and overwrite settings. |

The other single-run configuration fields use the same defaults and enum values shown
in the schema. Relative `copy_files`, `output`, and `pricing_file` paths are resolved
relative to the template YAML file. A template may contain `prompt` or `prompt_file`
when it was copied from a saved `run` configuration, but those fields are ignored when
`prompt_template` is present.

For editor completion and inline validation, install the **YAML** extension from
Red Hat (`redhat.vscode-yaml`) in VS Code. `template init` creates a
`test-wsl2-llm-template.schema.json` file beside the new YAML and writes its absolute
path into the YAML language-server header. Every `template run` also creates or
refreshes that file and header, so templates copied from elsewhere remain usable:

```yaml
# yaml-language-server: $schema=C:\\path\\to\\test-wsl2-llm-template.schema.json
```

For templates in another directory, the generated absolute header needs no adjustment.
Alternatively, associate the schema with all template files in the workspace's `.vscode/settings.json`:

```json
{
  "yaml.schemas": {
    "${workspaceFolder}/test-wsl2-llm-template.schema.json": [
      "**/*-template.yaml",
      "**/*-template.yml",
      "**/questions.yaml"
    ]
  }
}
```

The editor schema provides completion and catches malformed field types; run
`test-wsl2-llm template run TEMPLATE.yaml` to apply the complete runtime validation,
including duplicate question IDs, missing placeholders, model availability, and
validator arguments.

This writes `analysis-etmiss-MODEL-high-001.md` and matching YAML and copied-back
artifacts, then the corresponding files for `leading-jet-pt`. Every report name
includes the full model/effort selector, including for single-model runs. Punctuation
in the selector is normalized to hyphens for portable Windows filenames: `gpt-5.4:high`
becomes `gpt-5-4-high`. Periods in output stems and question IDs are normalized the
same way. Existing reports using the older names without a selector are left in place
and do not mark a matrix cell complete. With `repeat: 1`, the numeric suffix is
omitted. `threads` limits total simultaneous jobs across all models, questions,
and repetitions. Each report gets a heading `Question: <id> - <first 30 characters>...` using its `question` field; templates without a `question` field use the rendered prompt. A custom YAML `title` or CLI `--title` overrides this automatic heading. The command accepts the shared `run` options as CLI overrides,
including `--model`, `--output`, `--repeat`, `--threads`, and `--force`.

Templates accept the same run configuration keys as a normal saved configuration,
including `marketplaces`, `plugins`, `copy_files`, `copy_back`, sandbox, network,
approval, authentication, pricing, and cleanup settings. This supports the
command-line-to-template workflow:

```powershell
test-wsl2-llm run --prompt-file .\prompt.md --model MODEL:high `
  --marketplace https://github.com/example/marketplace.git `
  --plugin demo@example `
  --output .\results\trial `
  --save-config .\trial-config.yaml --config-only
test-wsl2-llm template init .\questions.yaml
```

Copy the shared keys from `trial-config.yaml` into `questions.yaml`, replace its
single-run `prompt` with `prompt_template`, and add `questions`. The saved `prompt`
field is accepted and ignored when `prompt_template` is present, so the copied
marketplace/plugin and execution settings continue to apply to every expanded job.

Use `--copy-file PATH` (repeatable) to copy Windows files into the root of the fresh WSL
workspace before Codex starts. This is useful for local credentials such as a
`servicex.yaml` file. The same option can be written in YAML as `copy_files`; paths are
resolved relative to the input YAML file. The resolved list is saved in the YAML
`configuration` section and in the Markdown report's expanded `Resolved configuration`
section. The copy operation itself does not add file contents to either report.

Use `--copy-back PATH` (repeatable) to copy files from the WSL workspace back to Windows
after Codex finishes. Relative paths and shell-style wildcards such as `plot_*.png` are
resolved from the workspace root. Each matching file is
written beside the reports as `<output-stub>.<file-name>` (for example,
`results\hello.output.png`), and the Markdown report links to every copied file. Images
are displayed with PNG previews embedded directly in Markdown and also have an ordinary
file link, text files show their first ten lines, and ROOT files are inspected with
`uproot` to list their objects plus TTree branches and event counts. The YAML form is
`copy_back`. Template questions may also provide a `copy_back` list; entries are added to
that question's shared patterns, while entries beginning with `-` remove an exact shared
pattern (for example, `-ab-output.root`). A removal must refer to a shared pattern or an
earlier addition in the same question; otherwise template loading fails with an error and the
YAML must be corrected before the batch can run. Template questions may also provide a
`plugins` list; entries are added to that question's shared plugin selectors, while entries
beginning with `-` remove an exact shared selector (for example, `-shared-tools@my-marketplace`).
A plugin removal must refer to a shared selector or an earlier addition in the same question.
This lets each question use a different plugin set while sharing the same marketplaces.
Template questions may also provide a `distro` string to select a different WSL
distribution for that question. It overrides the shared YAML `distro` only for the
expanded jobs from that question; the global `--distro` option still overrides every
question. Questions without a `distro` field inherit the shared value (or the WSL
default when neither is set).

If a requested copy-back path or glob has no matches, collection continues for the other
patterns. Missing patterns are listed in the YAML `missing_copy_back` field and in the
Markdown report's Copied-back files section.

At most 100 copied-back files are collected per run by default, preventing broad globs
such as `plot_*.png` from creating thousands of artifacts. Use `--max-copy-back N` (or
`max_copy_back_files` in YAML) to choose a different positive limit.

Use `--force` to replace an existing Markdown/YAML result pair. Reports include the exact invocation used to create them, with local wall-clock times in Markdown and UTC timestamps preserved in YAML.
Pass `--title "# My test result"` (or set `title` in YAML) to customize the Markdown heading.

Regenerate Markdown later from a saved YAML result without rerunning Codex:

```powershell
test-wsl2-llm generate .\results\hello.yaml `
  --output .\results\hello-summary.md `
  --no-details
```

The output defaults to the YAML file's stem. Use `--force` to replace an existing Markdown file.
### Portable Markdown viewing

Reports keep the outcome, prompt, final response, validation diagnostics, and artifact
links in ordinary Markdown so they remain readable in GitHub, VS Code, and other viewers
without JavaScript. The copy buttons and bounded HTML preview controls are best-effort
enhancements; disabling scripts removes those controls but does not remove the fenced
code, indented Markdown, or ordinary relative links. GitHub may sanitize embedded PNG data
URIs, so use the visible **Open file** link when an image preview is unavailable. Relative
artifact links work when the Markdown report remains beside its copied-back files; moving
only the report breaks those links. Viewer-specific styling, image previews, and HTML
details disclosure are not guaranteed across Markdown renderers.
Workspace inventory, Codex stderr, and model activity (as an elapsed-time table) are included by default. `--details` also
includes resolved configuration, raw stdout JSONL, trace timing, session traces, and conversation
history when available.

Connect to a retained run workspace for interactive exploration:

```powershell
# Start a new interactive conversation in the saved workspace.
test-wsl2-llm connect .\results\hello.yaml

# Resume the latest conversation retained by that run.
test-wsl2-llm connect .\results\hello.yaml --resume
```

The first form launches Codex with `codex --cd <workspace>`. Use `--access shell` to open a
login Bash shell in the retained workspace instead:

```powershell
test-wsl2-llm connect .\results\hello.yaml --access shell
```

The `--resume` form launches
`codex resume --last --cd <workspace>`, selecting the newest session in that run's retained
isolated Codex home. The run must have been created with `--keep-workspace` (or YAML `cleanup: false`).

Start a fresh, non-resumed conversation in the same retained workspace with a new prompt:

```powershell
test-wsl2-llm continue .\results\hello.yaml `
  --prompt "Review the file and suggest a next step." `
  --output .\results\hello-review
```

`continue` carries the prior prompt/final-response chain into the new Codex prompt, keeps the
previous configuration (model, sandbox, approvals, authentication, and reporting settings),
skills, and plugins, and appends any marketplaces or plugins supplied on the command line. The
new report's top-level prompt is only the new prompt; the complete chain is preserved in YAML and
in a Markdown details section. The previous result must retain its workspace.

The default Codex policy is `workspace-write` with network access, `on-request` approvals, and the `auto_review` reviewer. The normal WSL Codex home is not modified. Its `auth.json` is copied into an isolated run home with mode `0600` and removed at the end.

## YAML configuration

Before loading an explicit `--config` file or template, the CLI looks for the optional user
defaults file `~/.test-wsl2-llm/config.yaml`. The path can be overridden with the
`TEST_WSL2_LLM_DEFAULT_CONFIG` environment variable. The defaults file may be partial; values are
merged in this order: user defaults, explicit run/template configuration, then CLI options.
Nested `environment.unset` and `environment.path_remove` fields merge independently, while an
explicit list replaces the corresponding default list. Finding the defaults file emits an INFO
message, visible with `-v`.

```yaml
prompt: |
  Use $analysis-helper to create answer.txt.
title: "# WSL2 Codex test result"
model: MODEL:high
distro: atlas_al9
wsl_parent: ~/codex-tests
marketplaces:
  - C:\Users\me\Code\marketplace
  - https://github.com/gordonwatts/atlas-analysisbase-marketplace.git
plugins:
  - analysis-tools@my-marketplace
  - atlas-analysisbase@atlas-analysisbase-marketplace
copy_files:
  - .\servicex.yaml
copy_back:
  - output.png
environment:
  unset:
    - INCLUDE
    - EXTERNAL_INCLUDE
    - LIB
    - LIBPATH
  path_remove:
    - 'C:\Program Files (x86)\Microsoft Visual Studio\'
    - 'C:\Program Files (x86)\Windows Kits\*'
output: C:\Users\me\Results\analysis-run
progress_lines: 5
```

Run it and override individual fields on the command line:

```powershell
test-wsl2-llm run --config .\test.yaml --model MODEL:high
```

The optional `environment` policy filters the inherited Windows environment before every WSL
launch. `unset` removes variable names case-insensitively, including variables that would
otherwise cross through `WSLENV`. `path_remove` removes individual semicolon-separated Windows
`PATH` entries using case-insensitive prefixes or glob patterns. Both lists default to empty, so
existing configurations are unchanged. Use repeatable `--unset-env NAME` and
`--path-remove PREFIX_OR_GLOB` options to set either list from `run`, `template run`, or
`continue`. Saved configuration records only the policy names and patterns, never environment
values.

Create a reusable configuration from CLI inputs without invoking WSL:

```powershell
test-wsl2-llm run `
  --prompt-file .\prompt.md `
  --model MODEL:medium `
  --output .\results\trial `
  --save-config .\trial.yaml `
  --config-only
```

Use `-v` to see WSL/Codex commands and every Windows `PATH` entry removed before a WSL
launch. Use `-vv` to log every returned line instead of the bounded live display and to state
explicitly when no `PATH` entry matched the configured removal patterns. `connect` supports the
same `-v` and `-vv` PATH diagnostics.

Git marketplace URLs supplied with `--marketplace` or YAML are shallow-cloned into the fresh WSL run harness before installation. For example:

```powershell
test-wsl2-llm run `
  --distro atlas_al9 `
  --model MODEL:high `
  --prompt "Use `$analysis-base to describe an AnalysisBase work area." `
  --marketplace https://github.com/gordonwatts/atlas-analysisbase-marketplace.git `
  --plugin atlas-analysisbase@atlas-analysisbase-marketplace `
  --output .\results\atlas-analysisbase
```

To clone a non-default branch, append `@<branchname>` to the Git URL. Branch names
containing `/` are supported; the suffix is removed from the URL and passed to the Git
branch selector. The same syntax works for marketplace entries in YAML. For example:

```powershell
test-wsl2-llm run `
  --marketplace https://github.com/example/marketplace.git@feature/new-plugin `
  --output .\results\feature-marketplace
```

This branch suffix applies only to Git marketplace URLs. Local marketplace paths and
plugin selectors such as `plugin-name@marketplace-name` keep their existing meaning.

## Tests

Normal tests mock WSL and never spend model tokens:

```powershell
uv run pytest
uv run ruff check .
```

Two opt-in acceptance tests use the real `atlas_al9` distribution and Codex account:

```powershell
uv run pytest --run-wsl-acceptance
```

Override the acceptance model with `--wsl-model MODEL` or `TEST_WSL2_LLM_MODEL`. These tests are skipped unless `--run-wsl-acceptance` is present.

## Model pricing

The bundled [`model-pricing.yaml`](src/test_wsl2_llm/model-pricing.yaml) records exact-model token rates per million tokens. The private `gpt-5.6-luna` alias has no published per-token rate, so its bundled rates are deliberately `null`. Copy the file, enter verified input, cached-input, and output rates, and select it with `--pricing-file PATH`. Result YAML contains full-precision rates, token allocation, component costs, and aggregate cost; the Markdown costs preserve useful precision for positive sub-cent totals (and display unavailable separately from $0.00).

The normal progress display keeps a persistent `Latest meaningful activity` line above the five most recent events. Routine MCP polling entries remain in that bounded detail log without replacing the summary, and each event is prefixed with local `HH:MM:SS` receipt time. Use `-vv` when every returned line should be streamed.

Model arguments use `MODEL[:EFFORT]`. Omitting the suffix selects `medium`; supported values are `minimal`, `low`, `medium`, `high`, and `xhigh` (when supported by the selected model). Saved configuration and result YAML use the same canonical `model: MODEL:EFFORT` selector.

### Workspace lifetime

`run` and `template run` remove each temporary WSL run root by default after
writing reports and collecting copied-back artifacts, including failed, timed-out,
and interrupted runs. Reports record `workspace_retained: false` and no workspace
path after successful removal. Use `--keep-workspace` for interactive `connect`
or `continue` work. The existing YAML setting `cleanup: false` is the equivalent
opt-out; `cleanup: true` is now the default and is written by `--save-config`.
Older configurations with an explicit `cleanup: false` still retain workspaces.
`continue` always retains its new workspace, so another continuation is possible.
Cleanup failures are included in the report and leave the workspace marked retained.

If report writing fails, the workspace is preserved for recovery. Reports are then
updated after cleanup to record whether removal succeeded.
## Named MCP servers

Use repeatable `--mcp NAME` on `run`, `template run`, or `continue` to import a
server from the Windows user's `$CODEX_HOME/config.toml` (default
`~/.codex/config.toml`) into the isolated WSL Codex configuration:

```powershell
uv run test-wsl2-llm run --model MODEL --prompt "/mcp" --mcp my-server
uv run test-wsl2-llm template run batch.yaml --mcp my-server --mcp another-server
```

YAML input and saved configurations use names only:

```yaml
mcp_servers:
  - my-server
  - another-server
```

The complete selected `[mcp_servers.NAME]` tables are copied, including nested
settings, arguments, environment values, timeouts, tool filters, and unknown
future options. Unselected servers and unrelated local Codex settings are excluded.
The CLI list replaces the YAML list for fresh runs and templates; `continue`
inherits the previous list (a YAML override can replace it) and adds CLI names.
Duplicate names are imported once. Missing names, unreadable files, or invalid
TOML produce a failure report before a fresh WSL workspace is created.

Definitions are read at execution time; `--save-config` and result configuration
record only names, not server settings or credentials. `--config-only` saves names
without reading the local Codex configuration. Each repetition uses the same names.
Commands, paths, URLs, and environment references are copied unchanged and must
work from the chosen WSL distro; referenced environment variables and separate
OAuth login state are not copied. No local Codex configuration is modified.

To check discovery, run with `--prompt "/mcp" --mcp my-server` and inspect the
response, or connect to a retained workspace and use `/mcp` in the Codex TUI.
The latter is Codex's interactive server listing command; the batch harness sends
prompt text through `codex exec`. See the [official MCP configuration documentation](https://developers.openai.com/codex/mcp).
### Result validation

Configure checks in run/template YAML. All checks must pass, including repeated names:

```yaml
validators:
  - name: require_string
    arguments:
      string: "Analysis complete"
  - name: require_string
    arguments:
      string: "events processed"
```

`require_string` performs a case-sensitive literal search of the current final response,
captured stdout JSONL, and stderr. Unknown names or invalid arguments are rejected before
WSL starts. Checks run locally after result/file collection for runs and continuations.
YAML records each check in `validation`; Markdown shows PASS/FAIL diagnostics. Any failed
check makes the run fail with a nonzero CLI exit code; an existing execution error is retained.
With no validators, existing behavior is unchanged.

Python integrations can call `register_validator(name, ArgumentModel, callable)` in
`test_wsl2_llm.validation`. The callable receives the complete `TestResult` and validated
keyword arguments and returns `(passed, message)`. This includes all logs, workspace
metadata, and `copied_back` local destinations for reading full returned files. Exceptions
become failed checks, and remaining validators still run.

Validate a locally returned ROOT tree with:

```yaml
copy_back: ["result.root"]
validators:
  - name: root_tree
    arguments:
      file: result.root
      tree: events
      must_have: [pt, eta]
      can_have: [weight]
      cannot_have: [secret]
      no_other_leaves: true
```

`file` must exactly match a copied-back file's recorded source or local destination;
the validator reads only that local destination with uproot. `tree` may include a ROOT
directory path. TTree and RNTuple objects are supported. Names are exact recursive branch
names (including nested branch paths). Required branches must exist, optional branches may
exist, and forbidden branches must be absent. `no_other_leaves` rejects every branch outside
`must_have` and `can_have`; by default additional branches are allowed. Missing or unreadable
files/trees, non-tree objects, and mismatches produce failed checks with diagnostics.
Numeric output checks use `name: num_compare` with arguments `var_name`, `number`,
and `tolerance`, for example `{var_name: efficiency, number: 0.8, tolerance: "5%"}`.
The validator searches the same output as `require_string` for `var_name=number`,
allowing whitespace, signs, decimals, and scientific notation. At least one matching
assignment must agree. Numeric tolerance is an inclusive absolute difference; percentage
tolerance requires a relative difference strictly below the percentage, using the absolute
reference value. With a zero reference or zero tolerance, only exact equality passes.
Tolerances must be finite and nonnegative; expected numbers must be finite.
Markdown files are rendered inline as indented Markdown content in the report; other text files
remain available as a compact first-ten-lines preview.
### Experimental SSH command target

Issue #83 provides `SshTarget` for passwordless, noninteractive command execution through an existing OpenSSH configuration. Configure an SSH host alias (and optionally a user, port, and remote workspace parent); the adapter uses `BatchMode=yes` and a bounded `ConnectTimeout` while leaving host-key verification enabled. Saved target metadata contains only the alias and public connection settings; passwords and private keys are intentionally unsupported. Workspace transfer, remote ownership, collection, and cancellation are tracked separately in issue #84.
