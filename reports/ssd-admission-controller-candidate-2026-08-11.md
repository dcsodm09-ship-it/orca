# SSD Admission Controller candidate

Status: local-only P0 candidate. No Orca runtime, lifecycle call site, database,
credential store, Keychain item, session directory, socket, service, package, or
installed application was changed. The controller is read-only and performs no
create, start, archive, or restore mutation.

## Five live gates before implementation

All gates passed from the fixed candidate directory before any candidate file
was written:

1. Physical cwd: `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`, strictly
   below `/Volumes/Extreme SSD`.
2. Git common-dir: exact
   `/Volumes/Extreme SSD/Orca/projects/orca/.git`.
3. Fresh `/usr/sbin/diskutil info -plist /Volumes/Extreme SSD` returned exact
   Volume UUID `6EF3720E-C57F-4352-BA47-CFE88BFEFDA7`.
4. The same plist proved APFS, FileVault/encryption enabled, unlocked,
   `GlobalPermissionsEnabled=true`, writable, external, and solid-state.
5. `orca status --json` proved the app running, runtime reachable/`ready`, and
   graph `ready`.

The candidate repeats the volume, worktree/common-dir, and runtime/graph gates
for initial admission and again for the write-immediate recheck.

## Candidate contract

`AdmissionController.admit(operation, target)` supports `create`, `start`,
`archive`, and `restore`, but only returns a frozen read-only ticket.
`create`/`restore` require an absent target with an existing direct parent;
`start`/`archive` require an existing real directory. The caller must invoke
`recheck_before_write(ticket)` immediately before its own mutation.

The CLI applies the same two phases and emits JSON with
`mutation_performed=false`:

```text
/usr/bin/python3 scripts/ssd_admission_controller.py \
  start '/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca' --json
```

Admission fails closed on:

- a relative, non-normalized, `.`/`..`, out-of-boundary, or symlinked path;
- a wrong Git common-dir, volume UUID/mount point, filesystem, encryption,
  lock, Owners, writable/external/SSD state, or `st_dev` device;
- a same-name/stale remount whose live device identifier, device node, parent
  disk, mount inode, or mount device differs from the original ticket;
- caller-owner mismatch or group/world-write permission on the safe boundary
  and each existing directory below it;
- NFD-plus-casefold-equivalent sibling names;
- target state/identity drift, safe-boundary drift, or parent identity drift.

The parent identity is obtained through a read-only `O_DIRECTORY|O_NOFOLLOW`
descriptor and compared with the named path. The second phase repeats that
check and compares device, inode, mode, uid, and gid with the initial ticket.

The accepted target boundary is strictly below
`/Volumes/Extreme SSD/Orca`. Orca runtime databases, credentials, Keychain
material, session state, and active sockets remain on the internal system
volume and are neither located nor read by this controller.

## Verification

All test and Python cache writes were confined to the new current-user `0700`
SSD child:

```text
/Volumes/Extreme SSD/Orca/tmp/ssd-admission-candidate.K5pqgj
```

`TMPDIR`, `TMP`, `TEMP`, `PYTHONPYCACHEPREFIX`, and
`SSD_ADMISSION_TEST_ROOT` all pointed to that exact directory.

- `py_compile`: PASS for the controller and focused test.
- Focused suite: `17/17` passed in `1.058s`.
- Live read-only positive API preflight plus recheck: PASS.
- Live read-only CLI JSON preflight plus recheck: PASS; target identity stayed
  unchanged and both phases reported `mutation_performed=false`.
- Synthetic coverage: all four operation contracts plus wrong UUID, wrong
  common-dir, wrong device, stale same-name remount identity, symlink/`..`
  escape, owner mismatch, case-fold collision, graph-not-ready, and parent
  TOCTOU rejection.
- JSON parse/shape scan: PASS.
- Secret-pattern scan: PASS; no private-key, common provider-token, bearer, AWS,
  GitHub, Slack, or JWT signature pattern matched.
- Dangerous-call AST scan: PASS; no write/delete/install primitive, Keychain
  executable, write-capable `os.open` flag, `shell=True`, or destructive helper
  exists in the controller.
- `tabnanny` and tab/trailing-whitespace scans: PASS.

## Exact implementation identities

| File | Bytes | SHA-256 |
|---|---:|---|
| `scripts/ssd_admission_controller.py` | 24,048 | `c013d939530feb5c506f9350b88ca6bf4f196d300b73c24f9ebe2976ce28b1a6` |
| `tests/test_ssd_admission_controller.py` | 14,750 | `3eb498981fc74c39f58902f7b3bce0b1c5b1da69794ec91ba6788fc7a8c80934` |

The finalized report's own byte count and SHA-256 are recorded in the handoff;
embedding a report's self-hash would change that hash.

## Remaining installed-runtime limitations

1. No installed Orca lifecycle path calls this candidate. A green CLI/API
   result is advisory until each real create/start/archive/restore call site
   calls `admit` and then `recheck_before_write` at its mutation boundary.
2. The CLI performs both checks but cannot make an unrelated later process
   write immediately. The installed caller should retain an anchored parent
   directory descriptor and use descriptor-relative mutation to close the
   residual post-check race; this candidate closes its read-only descriptor.
3. Wrong-device, owner, case-fold, and TOCTOU negatives are isolated synthetic
   fixtures. No real volume was detached, renamed, replaced, locked, remounted,
   reformatted, or ownership-mutated for this task.
4. Same-name remount rejection is structurally covered with fresh plist fields
   and mount/path identities, but has not been exercised against a real detach
   and same-name replacement device.
5. The operation model is deliberately narrow: `create`/`restore` admit only a
   new direct child and `start`/`archive` only an existing directory. Real
   archive source/destination and restore-manifest semantics still require an
   explicit call-site mapping and separate integrity validation.
6. There is no packaged-app, long-running concurrency, crash/restart, or real
   lifecycle E2E evidence. This is not installed-runtime or production
   acceptance.
