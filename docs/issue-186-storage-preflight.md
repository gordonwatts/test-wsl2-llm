# Linux storage preflight: issue #186

The attached run log showed Codex 0.162.1 failing before a skill-reading command
started: `failed to register synthetic bubblewrap mount target /tmp/.git:
No space left on device (os error 28)`.

The affected Ubuntu WSL host's `/tmp` is a 16 GiB tmpfs. It still had approximately
11 GiB available, but all 1,048,576 inodes were allocated. This is a host storage
condition, so the harness should detect it before starting agents rather than
change sandbox permissions or move temporary storage elsewhere.

The preflight inspects `/tmp` and the actual testing parent with `statvfs`,
requiring at least 1,000,000,000 available bytes and one available inode in each.
It uses space available to the current account (`f_bavail`, `f_favail`), not
space reserved for privileged users. Canonical duplicate directory paths are
checked once; a missing parent is measured at its nearest existing ancestor.
Probe failures abort instead of being treated as healthy storage.

Validation on the WSL host is read-only and uses no model/API calls. The previous
sandbox failure had been reproduced directly with exit 101 while `/tmp` had
zero free inodes. By the time this preflight was validated, the host had changed
externally: `/tmp` now reported 16,657,219,584 available bytes and 2,097,131 free
inodes, and the new check correctly accepted it. Tests replaying the unhealthy
capacity condition verify that `run_test` raises `StoragePreflightError` before
any agent preflight or workspace creation, with the zero-inode value and required
capacity in the exception. This agent did not remove, relocate, or create host
temporary files to reproduce exhaustion.

Unit tests cover the exact decimal 1 GB boundary, zero inodes despite abundant
bytes, both directories, canonical-directory deduplication, missing parents,
the shared source probe across WSL/SSH/native Linux, and hard aborts before
agent/report/auth operations. Serial repeat and template batches demonstrate
that failure of the first cell stops all later queued cells from invoking the
runner. Continuation and connection tests cover retained testing locations.

Capacity checks are snapshots. Other processes can consume storage after a
successful check; the check does not reserve space or promise that an agent's
arbitrary outputs will fit. Native Windows and macOS are outside the Linux
preflight scope.
