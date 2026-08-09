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

Use `--force` to replace an existing Markdown/YAML result pair. `--overwrite` remains available as the equivalent configuration-oriented spelling.

The default Codex policy is `workspace-write` with network access, `on-request` approvals, and the `auto_review` reviewer. The normal WSL Codex home is not modified. Its `auth.json` is copied into an isolated run home with mode `0600` and removed at the end.

## YAML configuration

```yaml
prompt: |
  Use $analysis-helper to create answer.txt.
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
