# Wave 2 v4 archive-only candidate report

Generated from the SSD worktree on 2026-08-10T17:02:31Z (2026-08-11 Asia/Taipei).

## Result

The v4 candidate is implemented and synthetic-tested. It is **archive-only**: the program contains no file deletion primitive, never deletes a source, refuses overwrite publication, and exposes retirement only as a blocked `DELETE_NOT_AUTHORIZED` gate report.

This is not live or production acceptance. No archive command was run on any of the fourteen live Trash sources. No live source xattr was opened or written. The old script was read but never executed or modified. Neither `/Applications/Visual Studio Code.app/Contents/Info.plist` nor `/Applications/CapCut.app/Contents/Info.plist` was opened, written, copied, or hardlinked.

## Read-only five-gate evidence

All five gates passed before candidate work:

| Gate | Observed result |
| --- | --- |
| SSD working directory | `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca` |
| Git common-dir | `/Volumes/Extreme SSD/Orca/projects/orca/.git` |
| SSD identity | UUID `6EF3720E-C57F-4352-BA47-CFE88BFEFDA7` |
| Volume protection | APFS; FileVault enabled; unlocked; Owners Enabled |
| Orca readiness | runtime `ready`; graph `ready` |

The final read-only CLI preflight also exited 0 and reported `live_source_metadata_read=false` and `source_mutation_performed=false`.

## Frozen old-script preimage and fourteen-item allowlist

Read-only old-script preimage:

- Path: `/Users/www1adwawd/orca/recovery/cleanup-internal-green-wave2.sh`
- SHA-256: `2cc0c12183516d70513f09cb2ef24ed9e159f13813f8a5f6fb7eb88018b3ee7f`

The following order is frozen into the candidate and into every receipt:

1. `/Users/www1adwawd/.Trash/kiro-cli-installer-20260806.dmg`
2. `/Users/www1adwawd/.Trash/爱壹帆.app`
3. `/Users/www1adwawd/.Trash/ego-profile-probe.bz7Nav`
4. `/Users/www1adwawd/.Trash/ego-profile-probe.8zO6bP`
5. `/Users/www1adwawd/.Trash/ego-profile-probe.5L9Zje`
6. `/Users/www1adwawd/.Trash/isolated-profile-probe.eV8sCF`
7. `/Users/www1adwawd/.Trash/hgfast-ui-qa2.XkxtsR`
8. `/Users/www1adwawd/.Trash/hgfast-ai-support-browser-qa.dQLZGV`
9. `/Users/www1adwawd/.Trash/hgfast-support-ui-zcicAD`
10. `/Users/www1adwawd/.Trash/hgfast-ai-support-browser-e2e.deY0Hu`
11. `/Users/www1adwawd/.Trash/hgfast-ui-upgrade.cAMfAJ`
12. `/Users/www1adwawd/.Trash/node_modules 19-28-11-878`
13. `/Users/www1adwawd/.Trash/node_modules 19-10-21-092`
14. `/Users/www1adwawd/.Trash/node_modules`

## Candidate behavior

- Re-runs the exact worktree, common-dir, SSD UUID/APFS/FileVault/ownership, Orca runtime/graph, old-script SHA, target-count, uniqueness, and free-space gates.
- Requires `TMPDIR`, `TMP`, `TEMP`, and `PYTHONPYCACHEPREFIX` to be dedicated mode-0700 children of `/Volumes/Extreme SSD/Orca/tmp/` before archive work.
- Uses `ditto --rsrc --extattr --acl` into a unique `.work` copy, repairs only the isolated copy's symlink-inode mtime where macOS `ditto` does not preserve it, and exclusively renames the copy into the staging item path without overwrite.
- Rejects root symlinks, unsupported entry types, nested device boundaries, source/archive regular-file inode equality, changed sources, destination collisions, and any mismatch outside the narrowly audited fields.
- Lexically rejects symlinks aimed at either explicitly forbidden live App `Info.plist` before any target `lstat`, so target-type verification cannot touch those paths.
- Seals every relative entry's logical bytes, regular-file SHA-256, type, mode, nanosecond mtime, symlink raw text and observed target type, ACL raw digest, every ordinary xattr raw digest, ResourceFork raw digest, quarantine raw digest, and `com.apple.provenance` raw digest.
- Quarantine normalization can mutate only the SSD archive copy. The receipt preserves source raw seal, archive pre-normalization raw seal, archive post-normalization raw seal, and each copy-only action.
- `com.apple.provenance` is never normalized or hidden. Exact matches report `exact-raw-match`; differences report `AUDITED_APFS_PROVENANCE_DIFFERENCE`, retain both source/archive raw SHA-256 seals per path, record the copy-induced APFS audit reason, and explicitly say `audited-difference-not-zero-difference`.
- Re-seals each source after copy/normalization and requires its complete stability seal to match.
- Supports safe resume from already verified staging items. A mismatched same-name item blocks without overwrite. A complete fourteen-item set is required before publication.
- Publishes `VERIFIED.json` with mode 0600 using `renameatx_np(..., RENAME_EXCL)` plus file and directory fsync. Existing receipts are read-only revalidated for idempotency, must be bound to the current candidate-script SHA, and are never overwritten.
- Contains no source-retirement implementation. `retirement-gates` exits 77 with `DELETE_NOT_AUTHORIZED`.

The old v3 behavior that could count provenance differences and still emit `verification=zero-difference` is intentionally not carried forward.

## Synthetic fixture evidence

All synthetic work stayed under the dedicated mode-0700 directory:

`/Volumes/Extreme SSD/Orca/tmp/wave2-v4-final.wgWx6Q`

All descendant directories were checked after the runs; none had a mode other than 0700. The four temporary environment variables pointed to this SSD directory, and `PYTHONDONTWRITEBYTECODE=1` prevented Python cache-directory drift.

Targeted command:

```text
/usr/bin/python3 -m unittest -v tests.test_cleanup_internal_green_wave2_v4
Ran 12 tests in 7.103s
OK
```

Covered cases:

- exact fourteen-item freeze and old-script preimage;
- `LIVE_SOURCE_HARDLINK_FIXTURE_FORBIDDEN` L0 policy;
- pre-stat rejection for symlinks aimed at either forbidden live App `Info.plist`;
- no deletion/hardlink-creation primitives in the candidate AST;
- blocked retirement report;
- logical byte and content SHA seals;
- file type/mode/mtime;
- symlink text, target type, and symlink-inode mtime;
- ACL and ordinary xattr preservation;
- ResourceFork preservation;
- quarantine copy-only normalization with pre/post raw seals;
- synthetic provenance drift with both raw SHAs and no zero-difference claim;
- synthetic-only hardlink rejection;
- partial failure, breakpoint resume, atomic 0600 receipt, and idempotent verification;
- same-name collision with byte-for-byte proof that the pre-existing SSD item was not overwritten.

An idempotent CLI run against the already-created fourteen-item synthetic archive exited 0. Its receipt evidence was:

- Mode: `0600`
- Bytes: `56378`
- SHA-256: `630497248cd297828e1de995733b409a1fcb27c4c91f31ef47ab5684073b86ba`

Broader repository discovery before the final pre-stat L0 case ran 159 tests under the mandated SSD temporary root: 156 passed and 3 unrelated `test_index_processes` fake-executable tests failed. Those existing tests expect `shutil.which()` to accept an executable created inside `tempfile`; the external SSD runtime returned it unavailable. The v4 test module remained green in that run, and the final 12-test targeted rerun is the current-candidate result above. No attempt was made to change unrelated code or move those tests to `/tmp`, because this task requires every test temporary directory to stay on the SSD.

Whitespace checking of both new Python files produced no diagnostics.

## Artifact bytes and SHA-256

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/cleanup_internal_green_wave2_v4.py` | 42675 | `ae15ec958ec6a885ebec4d619844b1054ed781b32bf09433e63f2fa3c3bd96c8` |
| `tests/test_cleanup_internal_green_wave2_v4.py` | 14177 | `97395245d59d27c184fc32f0d9ec6613e8fcee979b265493efd84d4903ffb26d` |

The report's own exact bytes/SHA are supplied in the handoff after finalization; embedding a whole-file self-hash would change the hashed bytes.

## Runtime acceptance still blocked

No claim of live readiness, production readiness, source retirement, reclaimed space, or R2 recoverability is made. The remaining gates are:

1. Independent review of this candidate and its receipt schema.
2. A separately authorized archive-only run against all fourteen exact live sources, with fresh source stability evidence and enough SSD space.
3. Runtime review of any real APFS provenance differences, including each source/archive raw seal and the actual audited difference set.
4. Fresh R2 verification bound to the exact final archive receipt.
5. Fresh `lsof=0` evidence for every old path.
6. Fresh proof that new runtime cwd and Git common-dir resolve to the SSD.
7. Explicit authorization for the final built-in setup.
8. A separate, independently reviewed retirement implementation. v4 deliberately does not contain one.

Until every independent gate is satisfied, the only retirement result is `DELETE_NOT_AUTHORIZED`.
