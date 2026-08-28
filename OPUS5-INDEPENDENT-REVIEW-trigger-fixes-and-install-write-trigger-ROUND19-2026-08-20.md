# OPUS5 Independent Adversarial Re-Review — Round 19

**Candidate**: `完善orca/claude-codex-memory-bridge/` after fix rounds 1 and 2
**Date**: 2026-08-20
**Reviewer**: Claude opus5 (independent; a parallel Codex review ran on the same candidate with no coordination)
**Interpreter**: `/usr/bin/python3` — Python 3.9.6 (Clang 21.0.0), confirmed
**Method**: read every diff fresh at file:line, then *executed* the real registered command, mutation-tested the fix, corpus-verified the heuristic, and empirically stress-tested the timeout claim. Nothing below is taken from the fix report.

---

## Verdicts

| Piece | Verdict |
|---|---|
| Piece 1 — trigger fixes (P1-A compaction gate, P1-B registry fail-open) | **GO** |
| Piece 2 — CLI entry point / `install-write-trigger` P0 | **GO** |
| Piece 3 — diskutil timeout fix | **GO**, with one non-blocking P2 follow-up |

**No P0 or P1 findings remain.** I could not break any of the three fixes after substantial adversarial effort, including mutation testing designed specifically to prove the tests have real detection power. Findings below are P2.

> **A GO here does not authorize running `install-write-trigger`.** Installing/activating the SessionEnd handler on this machine remains the user's own separate, explicit decision, unchanged by this review.

---

## 1. P0 — registered SessionEnd command was a silent no-op: **GENUINELY CLOSED**

Both root causes verified fixed at source, then verified by execution.

**Sub-fix (a)** — `install_bridge.py:1386-1400`, `make_release()` now resolves the path itself and bakes `--write-candidates-root <path>` into the registered command. `os.fspath(RUNTIME_BASE / "write-candidates")` reads the module global at call time.
**Parser side** — `write_candidate_capture.py:2752-2775`, `_main_scan` accepts it as an optional 5th `--flag value` pair (`required_names` / `optional_names` / `known_names`), argv length bounded to 8 or 10, unknown or duplicate flags fail closed.

**Sub-fix (b)** — `write_candidate_capture.py:440-522`, `atomic_write()`. I read the **entire** function body: there is **no `import` statement of any kind** in it. Confirmed by grep across the whole module that the only remaining `install_bridge` import outside tests is `default_write_candidates_root()` at line 345, which the baked-in flag now bypasses.

### Independent mutation test (my own harness, not the project's test)

I built the real release under mocked constants, then ran the **actual registered command** as a real subprocess three ways:

| Case | Result |
|---|---|
| **A** — command exactly as registered | `rc=0`, `write-candidates/` created, **1 pending candidate, trigger T1** |
| **B** — same command, `--write-candidates-root` stripped | `rc=0`, empty stdout **and** stderr, `write-candidates/` **never created** |
| **C** — flag stripped **and** `install_bridge.py` planted into the release dir | still no-op |

Case B reproduces the original P0 failure signature **exactly** (exit 0, no output, no directory, forever). That is the proof the fix is load-bearing and that the project's own e2e test would actually catch a regression — not merely that it passes today.

Release-dir contents confirmed in all three cases: `claude_memory_hook.py`, `policy.json`, `write_candidate_capture.py` — **`install_bridge.py` is genuinely absent**, which is what made the lazy import fatal.

### Flake re-verification
`InstallWriteTriggerRealCommandEndToEndTests`: **18/18 passed** (10 runs, then 8 more at load average 60–81). No intermittent failures of any kind.

---

## 2. P1-A — compaction/resume summaries bypassing the gate: **CLOSED, corpus-verified**

Gate: `write_candidate_capture.py:1266-1267` (`_gate_g1b_system_injected_envelope`), fed by `:855` (`is_compact_summary=record.get("isCompactSummary") is True`).

I did not take the "84 real occurrences" claim on trust. I scanned this machine's real corpus:

- **325 transcript files, 421,697 records**
- `isCompactSummary == True`: **86** records — **all** `role: "user"`, **all** with `isMeta` absent and `origin` absent (exactly as the code comment asserts)
- Text-marker (`"This session is being continued from a previous conversation"`) occurrences: **86**
- marker **and** flag: **86** · marker **without** flag: **0** · flag **without** marker: **0**

**Exactly 1:1. Zero false negatives, zero false positives across the whole corpus.** The structural marker is strictly better than a prose match, and I found no record shape that defeats it. (86 vs. the reported 84 is just two more compactions since their measurement.)

---

## 3. P1-B — cross-project registry poison point: **CLOSED**

`_load_cross_project_registry_fail_open` (`write_candidate_capture.py:2493-2535`) catches `Exception` broadly, returns an empty registry plus a `degraded` flag; `scan()` calls it at `:2660`, inside the lock, after the `if not turns` early-out.

I attacked it with four corruption classes the existing tests do not cover:

| Attack | Result |
|---|---|
| Deeply-nested JSON (`RecursionError`, a `RuntimeError` subclass) | fails open, `degraded=True` — **does not escape** |
| Registry path is a **directory** | load fails open; `save` raises `WriteCaptureError` (see P2-b) |
| Mode `0644` (not private) | fails open, then **self-heals to `0600`** on save; next load clean |
| Registry path is a **symlink** to an outside file | fails open; **does not write through the symlink** — victim file untouched, symlink replaced by a real regular file |

The symlink result is a genuinely good security property (`os.replace` replaces the link itself). Self-healing is real, not just claimed.

**Fail-open direction is correct and does not lose protection that matters**: an empty registry makes `_register_and_check_cross_project_recurrence` return `False`, so a T3 candidate is *promoted* rather than suppressed. The failure mode is extra noise in `pending.jsonl`, not data loss, not a safety violation, and not a privacy leak (samples are redacted and truncated to 200 chars). The docstring's "secondary heuristic, not a safety invariant" framing is accurate.

**Test coverage claim verified**: `CrossProjectRegistryPersistenceTests` (`tests/test_write_candidate_capture.py:1160`) contains **exactly 10** test methods, on a layer that previously had zero.

---

## 4. Timeout fix — correct in direction, but its stated causal story is imprecise, and it is incoherent on one shared caller

### 4a. Is 15s justified? Empirically yes — but not for the stated reason.

I measured the real `diskutil info -plist "/Volumes/Extreme SSD"` call directly:

| Concurrency | n | median | max | >2s | >3s | >15s |
|---|---|---|---|---|---|---|
| 1 | 3 | 0.11s | 0.16s | 0 | 0 | 0 |
| 4 | 12 | 0.20s | 0.24s | 0 | 0 | 0 |
| 16 | 48 | 0.94s | 1.34s | 0 | 0 | 0 |
| 48 | 144 | **3.05s** | **5.12s** | 139 | 73 | **0** |

So the old `2s`/`3s` bounds **are** genuinely too tight under concurrent `diskutil` contention, and 15s carries ~3x headroom over the worst value I could produce. **The fix is correct.**

**But the fix report's causal story does not survive checking**, and this matters for anyone reasoning about it later:

- **Load average is not the driver.** At load average **50** and again at **81**, sequential `diskutil` ran at **0.11s median / 0.166s max** — nowhere near the reported 3.74s. The driver is *concurrent diskutil invocations* (~16+ way), not machine load.
- **I reverted both timeouts to their old values (2s / 3s) in a scratch copy and ran the e2e test 20 times: 20/20 passed.** The flake does not reproduce today in either configuration.
- The report's "full suite takes 84.9s under heavy load" is also not reproducible: my full-suite runs took **12.4s** at load average 81.

None of this makes the change wrong — the contention data independently justifies it. It does mean the *measurement narrative* in `claude_memory_hook.py:232-243` overstates what was demonstrated.

Could a heavier moment still exceed 15s? It would take roughly 3x the worst contention I could generate. Possible on this machine, but the consequence is fail-closed (a missed capture), not corruption — acceptable for SessionEnd. A retry-with-backoff would be strictly better than a single long bound, and was not considered; not blocking.

### 4b. **P2 (new finding): `timeout=15` is unreachable on the live UserPromptSubmit path**

This is the one thing the fix round did not account for.

`_disk_volume_uuid()` lives in `claude_memory_hook.py:224-259` and is shared by **two** callers with opposite latency contracts:

- **SessionEnd scan** — registered with `timeout=None`, i.e. no `timeout` key at all (`install_bridge.py:1956`, `:2406`). Unbounded by design. 15s is fine here.
- **UserPromptSubmit hook** — `claude_memory_hook.py:28` (`HOOK_EVENT = "UserPromptSubmit"`), reaching `verify_storage` at `:1476`. This handler is registered with `DEFAULT_HOOK_TIMEOUT = 5` (`install_bridge.py:49`, via `update_hook_config(raw, release["command"])` at `:1943`/`:2399`).

I confirmed this against the **live installed config**, not just the source — `~/.codex/hooks.json` carries `"timeout": 5` on the live bridge's UserPromptSubmit handler.

So on the live path the hook process is externally killed at 5s, and the new 15s internal bound **can never fire**. The justifying comment at `claude_memory_hook.py:238-241` cites the design doc's SessionEnd "unbounded-latency … nothing downstream depends on the handler returning quickly" intent — which is simply not true of the UserPromptSubmit caller of the same function.

Net effect of 2s → 15s on the live hook:

- **Win, 2s–5s band**: memory context now injects where it previously failed closed. This is real, and it is exactly the live-install concern raised as context — my contention data shows this band is genuinely reachable.
- **Loss, >5s band**: previously a clean fail-closed exit at ~2s; now a hard kill at 5s — up to 3s of extra prompt latency the user waits through, a possibly-orphaned `diskutil` child, and a runner-surfaced timeout instead of a silent graceful degrade.

**Recommended follow-up (not blocking)**: parameterize the bound per caller — roughly `4s` (just under the 5s handler budget) for UserPromptSubmit, `15s` or `None` for SessionEnd. That captures the entire win with none of the hard-kill regression. Note this also means redeploying the current 15s value to the live install would be a *partial* improvement, not a clean one.

### 4c. Other `volume_uuid()` callers — checked, no latency-sensitive caller

`install_bridge.volume_uuid()` (3s → 15s) has exactly two non-test callers: `make_release()` (`:1276`) and `verify()` (`:2182`). Both are one-shot interactive CLI actions (`install` / `plan` / `verify` / `install-write-trigger`) where a human ran the command deliberately. No hook, daemon, or automated caller. 15s only lengthens the worst case of an explicitly-invoked command. Acceptable.

---

## 5. Remaining P2 findings

**P2-a — `default_write_candidates_root()` resolves *production* paths regardless of test isolation.**
`write_candidate_capture.py:331-347` keeps the lazy `import install_bridge`. It is now dead on the registered path but still live for `_main_list_pending` (`:2810`). The hazard: when `install_bridge` *is* importable, it returns the real hardcoded `RUNTIME_BASE`, ignoring any constants a test has patched in the parent process — because the subprocess re-imports the unpatched module.

I hit this myself: my case-C probe wrote a synthetic candidate into the **real** `/Volumes/Extreme SSD/Orca/local-homes/.shared-runtime/claude-codex-memory-bridge/write-candidates/`. I verified the content was exclusively my own synthetic fixture data (session `4444…`, my invented transcript text — no real user content), confirmed the directory had not existed beforehand, and **removed it, restoring the pre-probe state**. Production is confirmed clean.

Suggested: delete the fallback and make `--write-candidates-root` required, so the path can only ever come from the installer.

**P2-b — a directory at the registry path permanently disables cross-project dedup, silently.**
`save_cross_project_registry` raises `WriteCaptureError` (verified empirically). Because `scan()` orders `append_candidates` (`:2667`) → `save_checkpoint` (`:2670`) → `save_cross_project_registry` (`:2671`), **candidates and checkpoint still persist** — no data loss, no machine-wide breakage. But the registry never heals, and `ScanResult.cross_project_registry_degraded` is never surfaced: `_main_scan` discards the return value and exits 0.

**P2-c — the SessionEnd path has effectively zero failure observability.**
Every failure is `except Exception: return None`, exit 0, no output, no log (`:2608`, `:2680`, `:2784`). This is precisely the property that let the P0 stay invisible indefinitely, and it is unchanged. `checkpoint.json`'s `scanned_at` is a partial heartbeat, but it is only written *after* all verification passes — so the failures that matter most (the P0 class: hash drift after a Claude Code update, a >15s diskutil, directory mode drift) still leave no trace anywhere. A one-line status breadcrumb would make this whole class detectable instead of requiring another manual review to find.

**Nit — latent double-close in `atomic_write`.**
`write_candidate_capture.py:503-521`: both `except` handlers call `os.close(descriptor)` after `os.fdopen(..., closefd=True)` (`:492`) has already closed it. Benign today — single-threaded, and the resulting `EBADF` is swallowed by `except OSError: pass` — but it is a real fd-reuse hazard if this module is ever threaded. Same shape as `install_bridge.atomic_write`, so pre-existing rather than newly introduced.

---

## 6. Suite and scope

**Test suite** (`/usr/bin/python3 -m unittest discover -s tests -v`, from `claude-codex-memory-bridge/`), run twice as requested:

- Run 1: **Ran 288 tests — OK** (12.354s)
- Run 2: **Ran 288 tests — OK** (12.488s)
- Load average during runs: **81.27 / 62.23 / 48.66** — i.e. green under genuinely heavy load, not a quiet-machine result.

**Scope** — `git status --short -- claude-codex-memory-bridge/` is exactly the expected set, nothing extra:

```
 M claude-codex-memory-bridge/claude_memory_hook.py
 M claude-codex-memory-bridge/install_bridge.py
 M claude-codex-memory-bridge/tests/test_claude_memory_hook.py
 M claude-codex-memory-bridge/tests/test_install_bridge.py
?? claude-codex-memory-bridge/AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md
?? claude-codex-memory-bridge/tests/test_nontrigger_corpus.py
?? claude-codex-memory-bridge/tests/test_write_candidate_capture.py
?? claude-codex-memory-bridge/write_candidate_capture.py
```

`prime-agent-integration/` and `round13-review-repros/` are unrelated and were ignored per instructions.

---

## 7. Plain statement

Given this candidate's history — the P0 claimed fixed and wasn't, twice, plus a flaky test that masked it — I held this to the highest bar in the cycle and deliberately tried to break it rather than confirm it: I mutation-tested the P0 fix to prove the test can actually fail, corpus-verified P1-A against 421,697 real records instead of trusting the reported count, attacked P1-B with four corruption classes its tests don't cover, and reverted the timeouts in a scratch copy to test the flake diagnosis independently.

**I could not break the P0, P1-A, or P1-B.** They are genuinely fixed. The timeout change is correct in direction and empirically justified, though its stated causal story overstates what was demonstrated and it left one real incoherence (4b) on the shared UserPromptSubmit caller — a tuning issue worth a follow-up, not a blocker.
