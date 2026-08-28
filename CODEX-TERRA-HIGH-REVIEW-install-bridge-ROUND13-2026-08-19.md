# Round 13 independent review — transactional installer

## Verdict

**GO** for frozen candidate `0ae1c0eaeadfcc3bf1d2d541acf5b6139b16d3d7`.  I found no reproducible P0/P1 in the round-13 change or its specified historical regression surface.

This was a read-only review of an isolated `git archive` extraction, because the shared worktree HEAD was `afc43fba4645d4af8e2d937b51cd70a53f2f02ce`, not the requested candidate.  The extracted `install_bridge.py` SHA-256 was `765b7ce8bd5e98b6674aea24ec025c02e1381d03b455a49bb8b83251270b784d`, equal to the hash of `git show 0ae1c0e:claude-codex-memory-bridge/install_bridge.py`; all fixtures used temporary pseudo-SSD trees, never the real `/Volumes/Extreme SSD` hooks configuration.  Startup was degraded (`ORCA_CONTEXT_NACK_V1`); no central Orca context is claimed.

## R12 oversized relocated-handler blocker

Independently fixed.  On both `/usr/bin/python3` 3.9.6 and PATH `python3` 3.14.6, I performed a real `install()`, moved account `nebula-omega-77` to the fresh deep destination `RUNTIME_BASE/backups/late-archive/wave-9/deep-relocation/nebula-omega-77`, and kept its genuine installed handler while adding a valid JSON `_migration_audit.blob` padding field.  The resulting plain UTF-8 `hooks.json` was 4,282,778 bytes and contained the bridge marker; `uninstall()` refused with the new oversized-runtime marker diagnostic, retained `latest-receipt.json`, created no pending journal, and left the file intact.

I also drove the recovery boundary rather than only its ordinary uninstall path.  A child process was genuinely killed with `SIGKILL` during the third `atomic_write` of an uninstall, leaving the durable journal; I then moved still-live `atlas-sequence-7` to `RUNTIME_BASE/quarantine/crash-window/final-pass/atlas-sequence-7` and padded its valid config above 4 MiB.  On both interpreters, `recover_pending_install()` and the `install()` entry point refused, preserving both journal and receipt; restoring the original content/path let recovery finish with `state == "uninstalled"` and delete the journal.  Thus the new check closes the silent-orphan state without creating a permanent recovery lockout.

The static route matches that behavior: `_CandidateTooLargeForDetection` is introduced at `install_bridge.py:63`; `_stream_scan_oversized_for_bridge_marker()` uses an `O_NOFOLLOW` descriptor, five encoded marker forms, a 1 MiB read size, and a `max(marker length)-1` overlap at lines 678-727; the only real `_read_for_detection()` caller catches that subclass first at lines 892-921.  Whole-file grep found no other executable callers/subclass catches, so ordinary `InstallError` behavior remains on the existing broad paths.

## Streaming scanner and scope probes

- For UTF-8, UTF-16LE, UTF-16BE, UTF-32LE, and UTF-32BE, three fresh placements each were detected: exactly one byte before the 1 MiB boundary, all but the last byte before the 2 MiB boundary, and wholly ending at the exact 1 MiB boundary (15/15 hits on both interpreters).
- Exact 2 MiB no-marker controls returned false.  At the smallest qualifying size (`MAX_MANAGED_FILE_BYTES + 1`), no-marker returned false and a marker-bearing file returned true.
- A sparse 160 MiB file with a UTF-32BE marker at a fresh late boundary was detected.  `ru_maxrss` grew only 983,040 bytes on 3.9 and 1,802,240 bytes on 3.14, not with the 160 MiB logical size; the scan is bounded in practice.
- An oversized no-marker `hooks.json` under `RUNTIME_BASE` was tolerated and ordinary uninstall returned `ok: true`.  Oversized no-marker and marker-bearing files outside `RUNTIME_BASE` both failed closed with the original `cannot safely inspect ... too large` diagnostic, and neither entered the new `oversized content under RUNTIME_BASE` route.

## New-test non-vacuity

I overlaid the three changed round-13 tests onto the frozen round-12 baseline `977dac84d4e81ff4fba8e3dd33c7888c9ae8e1e3` and ran each on both interpreters:

- `test_stream_scan_oversized_finds_a_marker_split_exactly_across_a_chunk_boundary` errors for the right reason: the baseline lacks `_stream_scan_oversized_for_bridge_marker`.
- `test_uninstall_catches_an_oversized_valid_relocated_handler_inside_its_own_runtime_tree` fails for the right reason: baseline `uninstall()` raises no `InstallError`.
- `test_uninstall_still_fails_closed_on_an_oversized_file_outside_runtime_base_even_with_a_marker` passes on both baseline and candidate, as intended by its unchanged-behavior control purpose.

## Historical regression checks

The three round-11 findings remain closed on both interpreters using fresh inputs: a 6,007-digit JSON integer gives `_contains_owned_handler(...) == False` without a bare `ValueError` (R11-P1-A); an independently relocated 90 KiB valid UTF-32LE handler beneath a fresh runtime release/quarantine depth refuses and retains the receipt (R11-P1-B); and a mode-0600 runtime directory is tolerated through uninstall with no pending journal (R11-P2-A).

The required hybrid tree, composed from the round-13 module plus `git show 6fb376c671:claude-codex-memory-bridge/tests/test_install_bridge.py`, passed **43/43** installer tests on `/usr/bin/python3` and **43/43** on PATH `python3`.  This includes its original real-SIGKILL install/uninstall recovery, transaction, relocation, permissions, ownership, receipt, and lock assertions.

## Full suite

From the isolated candidate extraction:

- `/usr/bin/python3 -m unittest discover -s tests -v`: **Ran 97 tests in 1.693s — OK**.
- `python3 -m unittest discover -s tests -v`: **Ran 97 tests in 1.509s — OK**.

No candidate code, tests, repository configuration, real hook configuration, install state, commit, or activation was modified.  The sole workspace artifact from this review is this report.
