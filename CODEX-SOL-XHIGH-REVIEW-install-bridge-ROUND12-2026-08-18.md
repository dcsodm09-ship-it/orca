# Independent read-only review — install bridge, round 12

## Scope and identity

- **Verdict: NO-GO** — one reproducible P1 remains.
- Reviewed the frozen object `977dac84d4e81ff4fba8e3dd33c7888c9ae8e1e3`, not the shared worktree HEAD (`67d140f5…`).  I extracted it into an isolated temporary directory; its `install_bridge.py` SHA-256 was `494f54267bb2c7df1bda58eecf35fd88523194796476f6ad3545c1895e45d5b0`, identical to `git show` for the candidate.
- No real `/Volumes/Extreme SSD` Codex configuration was read or changed.  All installs, relocations, permissions, crashes, and test fixtures below used independently-created temporary pseudo-SSD trees.  Startup context was degraded (`ORCA_CONTEXT_NACK_V1`); this report relies only on visible files and local execution.

## Blocking finding

### R12-P1-A — a >4 MiB live relocated handler inside `RUNTIME_BASE` is still silently abandoned

`_read_for_detection()` raises `InstallError` for a regular candidate larger than `MAX_MANAGED_FILE_BYTES` ([install_bridge.py:231](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:231)).  The newly broadened handler at [820-824](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:820) treats that exception as tolerated absence-of-evidence when the path is inside `RUNTIME_BASE`, and continues.  Consequently neither the raised RUNTIME structural bound at [833-835](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:833) nor Stage 2 can run.

Fresh end-to-end repro, on both `/usr/bin/python3` 3.9.6 and PATH `python3` 3.14.6:

1. Create isolated `.codex` and `codex-accounts/zephyr-91` hooks, then call real `install()`.
2. Move `zephyr-91` to `RUNTIME_BASE/backups/review-oversized-actual-handler/zephyr-91`.
3. Keep the installed owned handler intact and add a normal JSON string field of `MAX_MANAGED_FILE_BYTES + 16,384` bytes.  Resulting `home/hooks.json` is valid plain UTF-8 JSON, 4,211,634 bytes, and still contains the exact bridge marker.
4. Call real `uninstall()`.

Observed on both interpreters:

```text
over4m-size 4211634 limit 4194304
over4m-uninstall OK {"ok": true, ..., "unreachable": [".../zephyr-91/home/hooks.json"]}
over4m-receipt False orphan-marker True
```

The receipt is deleted while the relocated config and its executable owned handler survive.  The account is only classified as `unreachable` because the receipt path is absent; the safety scan is precisely the protection intended to detect the relocated live path and has silently skipped it.  This has the same durable orphan / `ok:true` end-state as R11-P1-B, requires no race or privilege, and is reachable by an ordinary backup/migration that pads a valid JSON config beyond 4 MiB.

**Fix direction:** do not classify oversized *regular `hooks.json`* candidates under `RUNTIME_BASE` as harmless absence-of-evidence.  At minimum, probe them with a bounded streaming structural/marker scan and fail closed if the marker is present; preferably use a parse strategy that can establish the owned handler without loading the entire file, or preserve the receipt and report a path-specific refusal.  A filename-only special case cannot be safe: this repro is a real relocated config at exactly that filename.

## Round-11 findings: independently rechecked

| Finding | Fresh probe and result on round 12 |
|---|---|
| R11-P1-A, huge JSON integer bare traceback | Used a different 6,007-digit integer, plus 2,503 nesting, malformed UTF-16BE marker bytes, an unpaired surrogate, and duplicate keys.  `_contains_owned_handler()` never bare-crashed on either interpreter.  3.14 returned `False` for the 6,007-digit case; the round-11 baseline errors with `ValueError` for its 4,301-digit regression vector.  A synthetic `RuntimeError` from the entire Stage-1 function is degraded to `False` without a marker and to clean `InstallError` with a marker, so the broad wrapper does not silently accept marker evidence. **Fixed.** |
| R11-P1-B, 65,536-byte bound inside runtime | Moved a different account (`zephyr-91`) four levels below `RUNTIME_BASE/releases/...`, wrote a valid UTF-32LE owned config of about 80 KiB, and uninstalled.  Both interpreters refused with `found an owned hook handler ...`, kept `latest-receipt.json`, and left the orphan untouched. **Fixed for files at or below 4 MiB; superseded by R12-P1-A above that cap.** |
| R11-P2-A, listable-but-unsearchable directory | Used mode `0600` (not the supplied test's `0400`) on a runtime backup directory containing `hooks.json`.  Both interpreters returned `ok:true`; no journal remained. **Fixed.** |

The new always-fail-closed Stage-2 policy also works: a deliberately unrelated runtime `hooks.json` containing a coincidental marker refused with a path-annotated `InstallError`.  That is an avoidable operator lockout (remove/rename the named debris), but it is diagnosable and consistent with the explicit round-12 policy; I grade it P2, not an additional blocker.

## Crash and cost probes

- Real child-process `SIGKILL` mid-uninstall (exit `-9`) followed by moving an account into `RUNTIME_BASE` with a different 307,862-byte valid UTF-16LE owned config: `recover_pending_install()` refused, preserving both pending journal and receipt; restoring the original file/path let recovery finish as `uninstalled` on both interpreters.
- Real `SIGKILL` followed by a fresh mode-`0600` runtime directory: recovery completed `uninstalled` and removed the journal on both interpreters.
- Performance: a safety scan over three 1.2 MiB valid non-owned `hooks.json` files plus one 3.6 MiB valid relocated owned handler under `RUNTIME_BASE` found exactly the owned path in 0.017 s (3.9) / 0.037 s (3.14).  This supports the bounded lock-hold claim below 4 MiB; it does not repair the P1 at the hard cap.

## Regression tests and suites

- Candidate full suite: `/usr/bin/python3 -m unittest discover -s tests -v` — **Ran 94 tests, OK**.  PATH `python3` 3.14.6 — **Ran 94 tests, OK**.
- The four changed round-12 tests each pass on both interpreters.  Executed against the exact round-11 source while retaining the round-12 test definitions: 3.9 has 2 failures + 1 error (the huge-integer behavior is version-sensitive); 3.14 has 2 failures + 2 errors, including the expected raw 4,301-digit `ValueError`.  Thus every new behavioral test is non-vacuous on at least one required interpreter, and all three end-to-end tests distinguish the baseline on both.
- Hybrid required by the task — candidate `install_bridge.py` with `git show 6fb376c671:claude-codex-memory-bridge/tests/test_install_bridge.py`: **51/52 pass on each interpreter**.  The sole error is the prior test named `test_uninstall_tolerates_an_unrecognizable_file_inside_its_own_runtime_tree`; its file contains the exact bridge marker and round 12 intentionally reverses that expectation.  The remaining 51 cover the requested prior journal, ownership-match, stat guard, baseline separation, lock, retired-account, pruned-backup, permission, relocation, and mode/owner protections without regression.

## Static call-site audit

`rg` found `_find_untracked_owned_configs()` has only the two expected callers: `recover_pending_install()` [1285](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:1285) and `uninstall()` [1797](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:1797).  Its scan-loop calls to `_is_regular_file`, `_read_for_detection`, and `_contains_owned_handler` are exactly [798](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:798), [820](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:820), and [835](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:835); `_is_regular_file`'s only unrelated caller is discovery at [328](/tmp/round12-review.H4BsPc/claude-codex-memory-bridge/install_bridge.py:328).  No unanticipated behavioral call site was found.

## Conclusion

Round 12 genuinely repairs the three listed round-11 findings in their stated size/error domains, passes both claimed 94-test suites, and retains the historical protections except for one deliberately superseded old tolerance test.  It nevertheless leaves a directly reproducible, silent `ok:true` orphan for a valid handler just above the same 4 MiB inspection cap that the new runtime-bound mechanism relies on.  Do not merge, install, or activate this candidate until R12-P1-A is fixed and re-reviewed on a new exact commit.
