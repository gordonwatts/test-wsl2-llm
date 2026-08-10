# test-wsl2-llm

`test-wsl2-llm` runs the Codex CLI from Windows inside a fresh WSL2 workspace. It copies Windows-side prompts and local plugin marketplaces into WSL, isolates Codex configuration while reusing a protected copy of existing authentication, and writes matching Markdown and YAML results.

## Install and Run

```powershell
uvx test-wsl2-llm run `
  --distro atlas_al9 `
  --model MODEL[:EFFORT] `
  --prompt "Create hello.txt containing Hello from WSL" `
  --output .\results\hello
```

This writes `results\hello.md` for people and `results\hello.yaml` for code. Both contain the prompt, configuration, skill locations, timing, token usage, workspace inventory, raw Codex JSONL/stderr, and collected Codex session traces.

Use `--force` to replace an existing Markdown/YAML result pair. Reports include the exact invocation used to create them, with local wall-clock times in Markdown and UTC timestamps preserved in YAML.
Pass `--title "# My test result"` (or set `title` in YAML) to customize the Markdown heading.

Regenerate Markdown later from a saved YAML result without rerunning Codex:

```powershell
test-wsl2-llm generate .\results\hello.yaml `
  --output .\results\hello-summary.md `
  --no-details
```

The output defaults to the YAML file's stem. Use `--force` to replace an existing Markdown file;
`--details` (the default) includes the trailing raw logs, traces, and workspace details.

Connect to a retained run workspace for interactive exploration:

```powershell
# Start a new interactive conversation in the saved workspace.
test-wsl2-llm connect .\results\hello.yaml

# Resume the latest conversation retained by that run.
test-wsl2-llm connect .\results\hello.yaml --resume
```

The first form launches Codex with `codex --cd <workspace>`. The `--resume` form launches
`codex resume --last --cd <workspace>`, selecting the newest session in that run's retained
isolated Codex home. The run must have been created without `--cleanup`.

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
output: C:\Users\me\Results\analysis-run
progress_lines: 5
```

Run it and override individual fields on the command line:

```powershell
test-wsl2-llm run --config .\test.yaml --model MODEL:high
```

Create a reusable configuration from CLI inputs without invoking WSL:

```powershell
test-wsl2-llm run `
  --prompt-file .\prompt.md `
  --model MODEL:medium `
  --output .\results\trial `
  --save-config .\trial.yaml `
  --config-only
```

Use `-v` to see WSL and Codex commands. Use `-vv` to log every returned line instead of the bounded live display.

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

The bundled [`model-pricing.yaml`](src/test_wsl2_llm/model-pricing.yaml) records exact-model token rates per million tokens. The private `gpt-5.6-luna` alias has no published per-token rate, so its bundled rates are deliberately `null`. Copy the file, enter verified input, cached-input, and output rates, and select it with `--pricing-file PATH`. Result YAML contains full-precision rates, token allocation, component costs, and aggregate cost; the Markdown cost table rounds USD amounts to the nearest cent.

The normal progress display retains only the five most recent lines and prefixes each with local `HH:MM:SS` receipt time. Use `-vv` when every returned line should be streamed.

Model arguments use `MODEL[:EFFORT]`. Omitting the suffix selects `medium`; supported values are `minimal`, `low`, `medium`, `high`, and `xhigh` (when supported by the selected model). The resolved model and effort are recorded separately in saved configuration and result YAML.
