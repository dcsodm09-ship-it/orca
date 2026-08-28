# Independent review — install_bridge round 14

## Scope and frozen identity

- Candidate reviewed: `f9a2d6605685bc16596ae49d9aa039a8a87802a5`, which was also the checked-out `HEAD` at review start.
- Baseline for the non-vacuity control: `0ae1c0eaeadfcc3bf1d2d541acf5b6139b16d3d7`.
- Reviewed change: `claude-codex-memory-bridge/install_bridge.py` (28 additions, 5 removals) and `tests/test_install_bridge.py` (27 additions). `git diff --check` passed.
- This was read-only with respect to the candidate: all action-level probes ran against separately `git archive`d source trees and temporary fake SSD/local-home trees. No real `/Volumes/Extreme SSD` hook configuration, install, activation, commit, or merge was touched.
- Startup context was degraded (`ORCA_CONTEXT_NACK_V1`); conclusions below use only visible local files and executed local evidence.

## Verdict: GO

No reproducible P0/P1 was found. R13-P2-A is fixed, the round-13 oversized-marker protection remains fail-closed, and no new blocking behavior was found in the widened scan-exception route. The only deliberate residual is the documented RUNTIME_BASE policy: if an oversized candidate cannot be inspected because its scan races, vanishes, loses read access, or changes identity, that candidate is treated as absence of evidence; outside RUNTIME_BASE the corresponding conditions remain fail-closed.

## Independent executed evidence

All fixtures used different account/path names from the candidate regression: account `quasar-38`, with RUNTIME_BASE quarantine path `backups/round14-independent/quartz-quarantine/hooks.json`; the SIGKILL fixture used `quasar-38` and a separate fake volume. The reviewer UID was 501, and each `chmod 000` fixture was first verified to reject `os.open()` with `PermissionError`.

| Probe | Python 3.9.6 | Python 3.14.6 | Result |
| --- | --- | --- | --- |
| Oversized (`MAX_MANAGED_FILE_BYTES + 123`) and unreadable RUNTIME_BASE file; `uninstall()` then re-`install()` | PASS | PASS | Both actions completed normally; no pending journal remained. |
| Real SIGKILL during uninstall after its durable journal and first revert; oversized+unreadable RUNTIME_BASE file remained present; `recover_pending_install()` | PASS | PASS | Child exited by SIGKILL, journal survived, recovery returned `uninstalled`, removed receipt/journal, and neither live config retained the bridge marker. |
| Oversized relocated live handler under RUNTIME_BASE | PASS | PASS | `uninstall()` refused with `oversized content under RUNTIME_BASE matches the bridge marker`, named the live path, and left receipt/no-pending state unchanged. |
| Oversized marker-bearing candidate outside RUNTIME_BASE | PASS | PASS | Refused with `too large` and named the outside path; no transaction state changed. |
| Small unreadable candidate outside RUNTIME_BASE | PASS | PASS | Refused with `cannot read` and named the outside path; no transaction state changed. |

The exact required workspace command also passed on both interpreters:

```text
cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v
Ran 98 tests in 1.212s
OK

cd claude-codex-memory-bridge && python3 -m unittest discover -s tests -v
Ran 98 tests in 1.056s
OK
```

I additionally ran a focused, two-interpreter four-test regression spot-check for R11-P1-A (huge JSON integer), R11-P1-B (large valid relocated RUNTIME_BASE handler), R11-P2-A (unsearchable RUNTIME_BASE directory), and the round-13 oversized relocated-handler case: `Ran 4 tests`, `OK` on each interpreter.

## R13-P2-A regression non-vacuity

The candidate's exact new test method, `InstallEndToEndTests.test_uninstall_tolerates_a_file_that_is_both_oversized_and_unreadable_inside_its_own_runtime_tree`, passed on both interpreters (`Ran 1 test`, `OK`). I then executed that same candidate test body with its imports intentionally bound to the archived round-13 baseline. It errors on both interpreters at the right pre-fix branch: `_read_for_detection()` raises `_CandidateTooLargeForDetection`, `_stream_scan_oversized_for_bridge_marker()` then gets `PermissionError` from `os.open`, converts it to `InstallError: cannot read .../oversized-and-unreadable/hooks.json`, and baseline `_find_untracked_owned_configs()` re-raises it from its old scan-exception handler. Thus the new test is neither vacuous nor merely checking a changed assertion.

## Static review of the widened catch

The changed catch is at `install_bridge.py:918-938`; the marker-hit refusal is still the following branch at `:939-943`, outside that catch. `_stream_scan_oversized_for_bridge_marker()` at `:699-727` can, on its real execution path, return `True`/`False`, raise its explicit `InstallError` on pre-open versus opened inode mismatch (`:712-713`), translate `lstat/open/fstat/read` `OSError`s to `InstallError("cannot read ...")` (`:707-724`), or let an `os.close()` failure from the `finally` escape. Exceptional allocation failures while constructing `window` are also ordinary `Exception` subclasses; `KeyboardInterrupt`, `SystemExit`, and other `BaseException` subclasses are not caught.

Accordingly the new catch does swallow more than plain EACCES: it intentionally also tolerates I/O failure, disappearance/replacement/identity-change races, close failure, and any ordinary unexpected scan failure, but only after the candidate was already established as both oversized and inside RUNTIME_BASE. This is consistent with the existing local policy for unavailable RUNTIME_BASE evidence and cannot swallow a completed marker hit, because a hit is a normal `True` return that reaches the still-fail-closed branch. It does not affect outside-RUNTIME_BASE candidates, which fail before this call at `:901-907`; both executed outside controls confirmed that boundary.

## Conclusion

The round-14 change restores the intended RUNTIME_BASE availability behavior without reopening the silent-abandonment path closed in round 13. The real SIGKILL recovery probe specifically showed no new recovery lockout and no false success while the conjunction persisted. Candidate `f9a2d6605685bc16596ae49d9aa039a8a87802a5` is GO for this review scope.
