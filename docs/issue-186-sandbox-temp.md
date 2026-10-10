# Sandbox temporary storage: issue #186

The issue attachment recorded Codex 0.162.1 failing before the skill-reading
command started:

```text
linux-sandbox/src/linux_run_main.rs:998:
failed to register synthetic bubblewrap mount target /tmp/.git:
No space left on device (os error 28)
```

On the same Ubuntu WSL environment, `/tmp` was a 16 GiB tmpfs with 11 GiB free,
but all 1,048,576 inodes were allocated. Several existing ATLAS job-options
checkout trees accounted for most of them. No existing `/tmp` data was deleted
or moved during investigation. The failure was inode exhaustion in sandbox
mount bookkeeping, rather than lack of permission to read the skill.

## Before and after

Validation used the real Codex 0.162.1 binary, the real `af-uchicago` skill,
and `codex sandbox`, without model/API calls. The harness-generated config and
launcher were exercised, substituting `codex sandbox` for the model-consuming
`codex exec` operation. The sandbox CLI needs an explicit
`-c sandbox_mode=workspace-write` for this probe.

| Configuration | Result with `/tmp` still at zero free inodes |
| --- | --- |
| Original workspace-write policy and ambient temporary storage | Exit 101; reproduced the exact `/tmp/.git` panic above |
| Only a run-owned `TMPDIR` | Bubblewrap still failed creating `/tmp/.git` |
| Only `exclude_slash_tmp=true` | Synthetic mount marker directory still failed under `/tmp` |
| Both settings, using `<run-root>/.harness/tmp` | Exit 0; read the skill header and successfully created a file in `$TMPDIR` |

The successful probe printed:

```text
---
name: af-uchicago
description: >-
```

Run-owned scratch storage must be a **sibling** of `codex-home`: putting it
inside `CODEX_HOME` allowed the skill read but made a scratch write fail with
`Read-only file system`, because Codex protects its own home. The sibling
location is writable through Codex's existing TMPDIR policy and avoids adding
scratch files to workspace inventories and copy-back glob results.

To repeat the minimal comparison on an already affected Linux host, set
`skill_path` to an existing skill and `run_tmp` to a new directory on a healthy
filesystem. Do not fill `/tmp` or delete unrelated files to manufacture a repro.

```bash
df -h /tmp
df -i /tmp
codex sandbox -c sandbox_mode=workspace-write -- head -n 3 "$skill_path"
mkdir -p -- "$run_tmp"
env TMPDIR="$run_tmp" codex sandbox -c sandbox_mode=workspace-write \
  -c sandbox_workspace_write.exclude_slash_tmp=true -- \
  sh -c 'head -n 3 "$1" && touch "$TMPDIR/write-probe"' sandbox "$skill_path"
```

This workaround addresses the observed sandbox dependency on exhausted shared
`/tmp`; it does not reclaim host disk space or inodes. A run parent on the same
exhausted filesystem can still fail. Applications that hard-code `/tmp` need to
honor `TMPDIR` to write scratch files. No full token-consuming metadata query was
replayed; the before/after check covers the failing sandbox operation itself.
