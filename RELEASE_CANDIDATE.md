# Release-candidate validation

The first release has an explicit support boundary in
[`release-support-matrix.yaml`](release-support-matrix.yaml). Rows marked
`supported` are release blockers and need a passing short live smoke. Rows
marked `experimental` are documented for early adopters but do not block the
first release. `deferred` rows are outside the contract and must not be
described as supported in release notes.

## Required live evidence

Run these checks from a clean checkout or an installed candidate. They require
the credentials for the selected target and may spend model tokens, so they are
not part of credential-free CI.

- Codex on Windows/WSL2: one success with a copied-back artifact and a passing
  validator.
- Codex with one local plugin and one named MCP server: one short smoke that
  proves both are present in the isolated run.
- Codex retained continuation: start with `--keep-workspace`, then run
  `continue` against the result YAML.
- Claude Code on WSL2: one non-interactive `claude --print` smoke when a Claude
  credential and model are available.

Also create a validation-failure result and a deliberately short timeout result
from the same candidate. These prove that failure reports are explicit rather
than silently passing. The expected process exit for both is non-zero.

Every result should be a paired `.yaml`/`.md` report. Keep the exact candidate
commit, package version, Python version, agent CLI version, target, and WSL
distribution with the review notes. Never put authentication files or their
contents in the result directory.

## Sanitized evidence bundle

Once the four required scenario YAML files exist, run:

```powershell
uv run python scripts/release_evidence.py `
  --output .\release-evidence `
  --result success=.\results\success.yaml `
  --result validation-failure=.\results\validation-failure.yaml `
  --result timeout=.\results\timeout.yaml `
  --result retained-continuation=.\results\continued.yaml
```

The command refuses missing or mismatched scenarios. It writes:

- `scenarios/<name>.yaml` and `scenarios/<name>.md`, with host paths and
  credential-like values redacted;
- `scenarios/<name>.artifacts.yaml`, containing artifact names, types, sizes,
  and errors but not artifact contents;
- `versions.yaml`, containing the exact harness, Python, agent, target, and
  distribution versions for each scenario; and
- `summary.yaml`, a compact machine-readable index for review tooling.

Inspect the rendered Markdown and sanitized YAML before sharing them. The
bundle is review evidence, not a package or a release publication.

## Release decision

Record the result for every row in the matrix. If a supported smoke fails, open
a focused blocking issue and leave the release decision open. Do not promote an
experimental or deferred row to supported merely because its unit tests pass.
No command in this document creates a tag, uploads a package, or publishes a
release.
