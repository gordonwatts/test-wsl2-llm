# Continuous integration

Pull requests and pushes to `main` run the credential-free CI workflow on
Ubuntu and Windows for Python 3.11, 3.12, and 3.13. The workflow runs Ruff,
deterministic tests, and a package job that builds both a wheel and source
distribution. The package job verifies that both artifacts contain the
license, schema, and pricing data, then installs the wheel into a temporary
environment outside the checkout, runs the installed CLI's help and
`template init` commands, and checks that the packaged resources are available.

The live WSL acceptance suite is reported as a separate, intentionally
excluded check. It requires Windows WSL2 and a real Codex account, so it is
never invoked by CI and cannot make model or paid calls. Run it locally with
`uv run pytest --run-wsl-acceptance` when those prerequisites are available.
