# test-wsl2-llm

`test-wsl2-llm` runs the Codex CLI from Windows inside a fresh WSL2 workspace. It copies Windows-side prompts and local plugin marketplaces into WSL, isolates Codex configuration while reusing a protected copy of existing authentication, and writes matching Markdown and YAML results.

## Install and run

```powershell
uvx test-wsl2-llm run `
  --distro atlas_al9 `
  --model MODEL `
  --prompt "Create hello.txt containing Hello from WSL" `
  --output .\results\hello
```

This writes `results\hello.md` for people and `results\hello.yaml` for code. Both contain the prompt, configuration, skill locations, timing, token usage, workspace inventory, raw Codex JSONL/stderr, and collected Codex session traces.

The default Codex policy is `workspace-write` with network access, `on-request` approvals, and the `auto_review` reviewer. The normal WSL Codex home is not modified. Its `auth.json` is copied into an isolated run home with mode `0600` and removed at the end.

## YAML configuration

```yaml
prompt: |
  Use $analysis-helper to create answer.txt.
model: MODEL
distro: atlas_al9
wsl_parent: ~/codex-tests
marketplaces:
  - C:\Users\me\Code\marketplace
plugins:
  - analysis-tools@my-marketplace
output: C:\Users\me\Results\analysis-run
progress_lines: 8
```

Run it and override individual fields on the command line:

```powershell
test-wsl2-llm run --config .\test.yaml --model MODEL
```

Create a reusable configuration from CLI inputs without invoking WSL:

```powershell
test-wsl2-llm run `
  --prompt-file .\prompt.md `
  --model MODEL `
  --output .\results\trial `
  --save-config .\trial.yaml `
  --config-only
```

Use `-v` to see WSL and Codex commands. Use `-vv` to log every returned line instead of the bounded live display.

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
