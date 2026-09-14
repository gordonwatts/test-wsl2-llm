# Release checklist

This checklist describes how to prepare a release without publishing one as
part of normal development work. The release owner should complete it from a
clean checkout after the release-candidate support matrix has been accepted.

1. Decide the version using the project's compatibility policy, then update
   `project.version` in `pyproject.toml`, `src/test_wsl2_llm/__init__.py`, and
   the matching `CHANGELOG.md` heading. Keep those values identical.
2. Review the changelog against the complete diff from the previous tag. Note
   breaking changes, supported Python versions, and any explicitly experimental
   targets.
3. Create an annotated Git tag named `v<version>` only after the version,
   changelog, tests, and support matrix have been reviewed.
4. Run `uv build` and `python scripts/verify_package.py dist`. Confirm that
   the wheel and sdist contain `LICENSE`, `template.schema.json`, and
   `model-pricing.yaml`, and that their filenames contain the intended version.
5. Install the wheel into a virtual environment outside the source checkout.
   Run the clean-install smoke commands in the README, including `--help`,
   `template init`, and one short authenticated smoke when the target is
   available.
6. Create the reviewed Git tag and, if release notes or downloadable artifacts
   are useful, a corresponding GitHub Release. The supported installation path
   is the tagged GitHub revision, for example:
   `uv tool install git+https://github.com/gordonwatts/test-wsl2-llm.git@TAG`.
   Do not upload the package to PyPI unless the distribution decision changes.
7. From a fresh directory, install the tagged revision with `uv tool install`
   (or run it once with `uvx --from git+URL`) and verify `--help`, `template
   init`, and the documented authenticated smoke. Retain artifact hashes and
   create the next `Unreleased` changelog section.

No step in this document publishes a release automatically.
