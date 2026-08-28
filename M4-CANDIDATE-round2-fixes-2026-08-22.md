# M4 candidate — round 2: all 16 findings (R1–R16) fixed

**Date:** 2026-08-22
**Candidate:** `orca-context-bridge/scripts/build_cross_project_catalog.py` + `orca-context-bridge/scripts/test_build_cross_project_catalog.py`
**Supersedes:** `CLAUDE-OPUS-MAX-REVIEW-m4-catalog-builder-fixes-2026-08-22.md` — **every one of its 16 findings (R1–R16, P2×3 / P3×6 / P4×7) is resolved in this candidate.** That review's NO-GO applied to the previous hashes and no longer applies to these.

> ### Numbers in this report are a snapshot, not an invariant.
> The live-fleet figures below (146 scan targets, 16 capabilities, 42 pages, 7 wiki projects, 27 repos / 143 worktrees) are a **live moving target** — this worktree has concurrent sessions committing to it, and `orca repo list` / `orca worktree list` change under you. `HEAD` moved from `223ace9000` (at review time) to `8f35de0963` during this fix round alone. **Re-derive every fleet number at review time; do not diff against these.** What should still hold at review time are the *invariants*, listed under "Live-fleet re-verification" below.

---

## Integrity gate

| Artifact | sha256 | State |
|---|---|---|
| **`build_cross_project_catalog.py`** (was `8ef05cff…ff774c77`) | **`826d558882c57e7fd316e6cb4f909f1ce54ded850033708a54e2c4adeab99834`** | changed, 1629 → 1995 lines |
| **`test_build_cross_project_catalog.py`** (was `f4fc7ccb…8725c6ea`) | **`75537dbbedc9f6a3c84b0c754dbbb1021a7f4710beffdb3abfb1a9483cd45c92`** | changed, 1616 → 2221 lines |
| Trust anchor `~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py` | `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` | **unchanged** ✅ (verified at start, mid-run, and at end) |
| Pilot `完善orca/wiki/reusable-capabilities.json` | `7de6af0f6827b21b81546914c84f992d211d575ed429658bbedc49596d875d18` | **byte-identical** ✅ `{"ok": true, "errors": []}`, 11 caps |
| Pilot `projects/rn邮箱/wiki/reusable-capabilities.json` | `d08fb975e71dce0f7724f1c017bd0c5672b2ecbe67d9a3dad7b1ae8473aad398` | **byte-identical** ✅ `{"ok": true, "errors": []}`, 5 caps |

- **Nothing committed, nothing staged.** `git diff --cached` empty; both candidate files remain untracked (`??`). `HEAD = 8f35de0963c9f6324ccf39568ad8c1b488ce6370` (advanced from `223ace9000` by *other* concurrent sessions during this round — the known pattern in this worktree, outside this candidate's blast radius).
- **Nothing deployed.** `build_cross_project_catalog.py` still does not exist anywhere under `~/.agents/skills/`.
- **No wiki file touched.** Neither pilot's `reusable-capabilities.json` nor any `orca-context-wiki.json` was modified; `validate_reusable_capabilities.py` was **not** modified (read-only import target only).
- **No stale lock.** `/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/` contains only `catalog.json`.

---

## The 16 fixes

### P2 — the three blockers

| ID | Fix | Where |
|---|---|---|
| **R1(a)** | Capability `id` is now validated against the **shared** grammar: `ID_RE` is **imported** from `validate_reusable_capabilities` (same anti-drift policy the module docstring already states for `derive_expected_project_id`), and `not ID_RE.match(cid)` routes violators into the existing `dropped` → `dropped_count` → `status:"partial"` → `degraded[]` → exit 1 machinery, exactly as P3-7 did for `kind`. `ID_RE` forbids `:`, so an id of `page:home` can no longer mint a global_id identical to a page's. | module `:92` import; `summarize_reusable_capabilities` id check + a 20-line comment naming both reasons the grammar is load-bearing |
| **R1(b)** | Page `global_id` uniqueness gets the **same two-pass count/flag treatment** as capability global_ids: `page_global_id_counts` → `duplicate_page_global_id: bool` on every page row → new top-level `ambiguous_page_global_ids[]` + `counts.ambiguous_page_global_ids`. Duplicated pages are **flagged and retained** (never dropped, never degrading the source), with the asymmetry-vs-capabilities decision spelled out: this tool does not own `orca-context-wiki.json`'s schema, so it flags rather than invents a drop rule. | `assemble_catalog`, new block immediately before the capability two-pass |
| **R1(c)** | The false `:500-504` comment is **rewritten**: it now states that disjointness is *enforced* (naming the actual check), separates disjointness-across-namespaces from uniqueness-within-one, and points at the new page duplicate detection. `page_count`/`counts.wiki_pages` are explicitly documented as counting **rows, not distinct pages**. | `summarize_context_wiki` |
| **R1(d)** | The vacuous B1 test is joined by **two real adversarial tests**: `test_b1_a_capability_id_that_would_collide_with_a_page_gid_is_dropped` (cap `id:"page:home"` + page `id:"home"`, plus `UPPER` and `trailing-`) and `test_b1_b_duplicate_page_ids_are_flagged_and_retained_not_dropped` (two pages sharing `id`, incl. the measured `{gid: page}` consumer-side loss). | test file |
| **R2** | `os.O_NONBLOCK` added to `_read_previous_catalog`'s open. **Verified both directions** (see below): fixed = returns in 0.371 s; counterfactual with the flag stripped = still blocked after 8 s, SIGKILL required, `.catalog.lock` orphaned. Docstring rewritten to say *why* `O_NOFOLLOW` and `S_ISREG` could not help (a FIFO is not a symlink; the guard sits downstream of the blocking call) and to state the now-true guarantee plainly. **Bonus taken, not skipped:** the same one-flag hardening applied to `read_one_source`'s open, closing the `isfile()`→`open` TOCTOU window on the read side. Established behaviour unchanged — an already-present FIFO is still rejected by the `isfile()` pre-check with the same `not_a_regular_file` reason. | `_read_previous_catalog`, `read_one_source` |
| **R3** | Two dedups, both recorded, neither silent. (1) **Exact-duplicate raw strings** are removed in `summarize_reusable_capabilities` before any edge is credited (mirroring the validator's `duplicate_depends_on` error), counted per-capability as `duplicate_depends_on_count` and fleet-wide as `counts.duplicate_depends_on_entries`. (2) **Two different legal spellings of the same target** from one capability (`"script:t.py"` + `"own:script:t.py"`) now credit one edge; the second entry stays `resolved` but carries `duplicate_edge: true`, counted as `counts.duplicate_edges`. Result: `in_degree == len(referenced_by)` is now a total invariant, and both count *distinct* edges — consistent with the already-deduped `referencing_project_ids`. Deliberately **not** treated as a dropped entry / `partial`: dedup here is lossless, unlike a drop. | `summarize_reusable_capabilities`, resolution loop |

### P3 — all six

| ID | Fix | Where |
|---|---|---|
| **R4** | `name` is validated by **round-tripping through this module's own grammar** — `_name_is_addressable(kind, name)` asks `_parse_depends_on_ref(f"{kind}:{name}")` and requires the parse to return what went in. That mirrors the validator's `bad_name` rules (no `:`, no leading/trailing whitespace, no newline/tab/line-separator/NUL) while making drift structurally impossible, and it is the property that actually matters: *can any legal depends_on string address this?* Violators go through the same `dropped_count`/`partial` machinery as R1(a) and P3-7. `NAME_MAX_LEN` deliberately not mirrored (a style bound; `_parse_depends_on_ref` imposes no length limit, so a long name is genuinely addressable here). | new `_name_is_addressable()` beside `_parse_depends_on_ref`; call site in `summarize_reusable_capabilities` |
| **R4 (`project_id` half)** | **Verified — and it is *not* structurally safe**, contrary to the "may already be safe" hypothesis: `project_id` comes from a path segment (or a basename fallback), so a directory literally named with a `:` yields a 4-part ref_key `_parse_depends_on_ref` can never produce. **But excluding on it would be wrong** and would break working resolution: both sides of a *same-project* ref build the key identically (`f"{project_id}:{kind}:{name}"`), so same-project resolution works regardless — only a *cross-project* ref to such a project is unspellable. Recorded rather than enforced: `project_id_addressable: bool` per capability + `counts.capabilities_with_unaddressable_project_id`. Live fleet: 0. | capability key-assignment pass |
| **R5** | New unresolved reason **`"capability-ambiguous"`** with `target_project_state: "in-catalog-target-excluded"`, fired when the target's ref_key was excluded for a **non**-ref-key-duplication reason (per R7's instruction). Additionally every unresolved row now carries **`target_excluded_reason`** (`null` when the ref_key was never an index candidate), so the genuinely-duplicated-ref_key case — which keeps reporting `capability-not-found`, as specified — is *also* no longer a silent misdiagnosis. | resolution loop classifier, inserted as a new arm 4 |
| **R6** | Self-reference is now detected **structurally and first**: `if ref_key == cap["ref_key"]`, a plain string comparison, **before** the forward index is consulted at all. `target_global_id` comes from `cap["global_id"]` rather than an index lookup that may legitimately be absent. This also matches the validator, which likewise tests self-reference before looking the target up among the file's own `(kind, name)` pairs. | resolution loop |
| **R7** | The overloaded list is **split into two clearly-named concepts**: `ambiguous_ref_keys[]` now means **only** "this literal ref_key string is claimed by 2+ capabilities" (multiplicity ≥ 2, the true P2-1 case); the new **`excluded_ref_keys[]`** is a list of `{ref_key, reason}` covering **every** bar, with reasons `duplicate-ref-key` / `duplicate-global-id` / `ambiguous-project-id` (highest-priority reason wins, so the reported reason is order-independent). New `counts.excluded_ref_keys`. **Judgment Call A's crash-prevention property re-verified by re-running the KeyError counterfactual** — see below. | capability key-assignment pass; test `test_r7_…_are_different_sets` shows both shapes in one run |
| **R8** | `summarize_cli_inventory()` gets a degrade path symmetric with the other two summarizers: a document carrying **none** of `schemaVersion`/`commandCount`/`verificationCounts`/`liveProbe`/`commands` → `status: "schema-invalid"`, no `cli_inventories[]` row, not counted in `inventory_projects`, into `degraded[]`, exit 1. Deliberately permissive (any **one** key suffices), so a genuinely empty-but-real inventory `{"schemaVersion": 1, "commandCount": 0}` still passes — pinned by its own test. Return type widened to `dict | None`. `version` excluded from the key set as too generic to be evidence. | `summarize_cli_inventory` + `_INVENTORY_SCHEMA_KEYS` |
| **R9** | **`--i-understand-output-override` deleted outright** — flag, handling, and `getattr` guard. The pin is now unconditional straight-line code. All 6 test call sites migrated to the `DEFAULT_OUTPUT_DIR` monkeypatch `OutputPinningTests` already used (`EndToEndTests.setUp` saves/restores it; `_run` no longer passes any flag). `test_override_flag_bypasses_the_pin` and `test_symlink_rejection_applies_even_under_the_override` are replaced by `test_no_output_override_flag_exists_at_all` (argparse rejects it with exit 2 + "unrecognized arguments"; no such dest on the parsed namespace; and the concrete write into a live project tree it used to permit is now refused with the tree byte-identical) and `test_symlinked_pinned_root_itself_is_still_rejected`. `test_u2_fatal_payload_…`'s blocker moved inside the pinned root so it still exercises the exit-4 boundary rather than stopping early at exit 2. | `build_parser`, `cmd_build`, test file |

### P4 — all seven (none deprioritized)

| ID | Fix | Where |
|---|---|---|
| **R10** | `_contributing()`'s docstring rewritten to state what it actually means — "projects whose copy of this file was **usable** (parsed, right shape, survivors kept)" — explicitly noting the degenerate all-dropped case is counted, and why `ok`-only is the strictly worse alternative. No behaviour change (Judgment B stands). | `assemble_catalog._contributing` |
| **R11** | Resolved by R9, and confirmed: the module docstring now says `--output` is **PINNED UNCONDITIONALLY … no invocation — documented or otherwise**, with no override caveat, and names the test mechanism (rebinding `DEFAULT_OUTPUT_DIR`) as a property of the test process rather than the CLI surface. | module docstring |
| **R12** | Docstring scope **narrowed to what is provable**, and empirically re-verified: the guard covers (a) every later import — the sibling one that matters — and (b) this module's own bytecode in the **script** form (`__main__` is never cached); it does **not** and structurally **cannot** cover this module's own `.pyc` when the module is **imported** as a library, because the import system caches around executing the body. Measured on a staged copy with `PYTHONDONTWRITEBYTECODE` unset: script form → no `__pycache__` at all; import form → `build_cross_project_catalog.cpython-314.pyc` written and `validate_reusable_capabilities.cpython-314.pyc` correctly **not** written (which is the guard working for its real scope). Accepted with the reason stated inline. | module docstring `:83-90` region |
| **R13** | **Verified, no change needed.** `git check-ignore -v` confirms `.gitignore:1  __pycache__/` covers both `orca-context-bridge/scripts/__pycache__/` and `orca-context-bridge/scripts/__pycache__/build_cross_project_catalog.cpython-314.pyc`. | — |
| **R14** | `except Exception` → **`except BaseException`**, reported through the same `_emit_error` boundary, then **re-raised** for `KeyboardInterrupt`/`SystemExit` (returning 4 for everything else). Cleanup is unaffected: `cmd_build`'s `finally: release_lock(...)` and `atomic_write_within`'s `except BaseException` tmp bracket both run before the exception reaches this frame. argparse's own `SystemExit` (`--help`, bad args) is outside the `try` and unaffected — pinned by the still-passing help test. | `main()` |
| **R15** | `test_t12_toctou_planted_symlink_…` **rewritten to drive the real `atomic_write_within()`**. `time.time` is pinned via `mock.patch.object` so the production tmp name `.catalog.json.tmp-{pid}-{ms}` is predictable and the symlink can be planted at exactly the path the function is about to claim; asserts `FileExistsError` from the real function, victim untouched, `catalog.json` still `b"original"`, and the planted link **surviving** (the open failed before the cleanup bracket, and this process must never unlink a path it did not create). A sanity assertion confirms the pinned clock really aims the function at the planted path, so the test cannot pass vacuously. | test file |
| **R16** | **9 hygiene defects fixed** (the review named 5): removed `assertNotEqual(state,"resolved")` after `assertEqual(state,"self-reference")`; removed `assertIsNotNone(x)` after `assertIs(x,False)`; removed `assertNotEqual(reason,"project-has-no-…")` after `assertEqual(reason,"capabilities-file-invalid")`; deleted the vacuous "first gamma ref if any else None → assert None" block (its contrast already has its own test) and replaced the comment with an explanation of why it could not fail; corrected the **factually wrong** `dont_write_bytecode` comment claiming the marker "also appears in the module docstring" (it occurs exactly once in the file — now asserted). Plus 4 found while there: `assertIn(status, ("partial","ok"))` tightened to the deterministic `"partial"` + the actual guard reason; two `read_only_from` call sites that discarded `checked` without asserting it; two unchecked exit codes. | test file |

---

## Re-verification

### 1. `py_compile` — clean on both files, both interpreters
```
python3 = Python 3.14.6, /usr/bin/python3 = Python 3.9.6
py_compile OK on 3.14 and on 3.9
```

### 2. Full suite — **86/86 pass, 0 failures / 0 errors / 0 skips** (was 69/69)
```
$ cd /Volumes/Extreme SSD/Orca/workspaces/orca/完善orca
$ python3 -m unittest orca-context-bridge/scripts/test_build_cross_project_catalog.py
......................................................................................
----------------------------------------------------------------------
Ran 86 tests in 9.242s

OK

$ /usr/bin/python3 -m unittest orca-context-bridge/scripts/test_build_cross_project_catalog.py
......................................................................................
----------------------------------------------------------------------
Ran 86 tests in 9.879s

OK
```
No `skipUnless` fired: both `RealPilotFileTests` ran against the real pilot files.

**+17 tests**, all of them new coverage for this round's findings:

`test_b1_a_capability_id_that_would_collide_with_a_page_gid_is_dropped` · `test_b1_b_duplicate_page_ids_are_flagged_and_retained_not_dropped` · `test_r2_fifo_previous_catalog_returns_instead_of_hanging_forever` · `test_r2_o_nonblock_is_actually_on_the_open_flags` · `test_r2_read_one_source_open_requests_o_nonblock` · `test_r3_duplicate_depends_on_string_credits_one_edge_and_is_recorded` · `test_r3_two_spellings_of_the_same_target_credit_one_edge` · `test_r4_unaddressable_names_are_dropped_not_silently_indexed` · `test_r4_project_id_addressability_is_recorded_for_every_capability` · `test_r5_reference_to_an_excluded_target_is_not_reported_as_not_found` · `test_r5_genuinely_absent_capability_still_reports_not_found` · `test_r6_self_reference_survives_its_ref_key_being_barred_from_the_index` · `test_r7_excluded_ref_keys_and_ambiguous_ref_keys_are_different_sets` · `test_r8_garbage_inventory_file_is_schema_invalid_not_a_healthy_zero` · `test_r8_minimal_but_real_inventory_still_passes` · `test_r14_keyboard_interrupt_is_reported_then_reraised` · `test_r14_ordinary_exception_still_becomes_exit_4_not_a_traceback`
(plus `test_no_output_override_flag_exists_at_all`, `test_symlinked_pinned_root_itself_is_still_rejected`, `test_help_advertises_output_and_no_override` and the rewritten `test_t12_toctou_…` replacing deleted override/OS-level tests.)

### 3. Trust anchor — unchanged
`50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` ✅ (start, mid-run, end)

### 4. R2 proved in both directions, through the real build path

Driven through `main() → cmd_build() → _read_previous_catalog()` with `DEFAULT_OUTPUT_DIR` pointed at a scratch dir (there is no `--output` override any more, so this is now the only honest way to run it off the live manifest — the live `catalog.json` was never made a FIFO):

```
=== FIXED (O_NONBLOCK present) ===
{"exit": 0, "elapsed": 0.371, "lock_left": false, "still_fifo": false}

=== COUNTERFACTUAL (O_NONBLOCK stripped), 8s hard cap ===
STILL BLOCKED AFTER 8s -> SIGKILLed (reproduces the hang)
--- lock left behind by the wedged run? ---
.catalog.lock      <-- the exact "every later run gets lock_held" cascade
catalog.json
```

### 5. Judgment Call A re-verified (required by R7)

The R7 split must not weaken the bar. Counterfactual re-run against the **new** code with only the `gid_dup` term removed:

```
COUNTERFACTUAL with gid_dup bar REMOVED -> KeyError 'pa#twin'
  <-- the bar IS load-bearing; Judgment A confirmed, unchanged by the split
```

### 6. Mutation test — the new assertions genuinely bite

A passing suite proves nothing if the new assertions cannot fail. 15 mutants, each reverting one fix on a staged copy, **all 15 killed** — and each by the specific test written for it:

```
KILLED               M1  revert ID_RE check (R1a)
                     killed by: test_b1_a_capability_id_that_would_collide_with_a_page_gid_is_dropped
KILLED               M2  page dup flag always False (R1b)
                     killed by: test_b1_b_duplicate_page_ids_are_flagged_and_retained_not_dropped
KILLED               M3  revert depends_on dedup (R3a)
                     killed by: test_r3_duplicate_depends_on_string_credits_one_edge_and_is_recorded
KILLED               M4  revert edge dedup (R3b)
                     killed by: test_r3_two_spellings_of_the_same_target_credit_one_edge
KILLED               M5  revert name check (R4)
                     killed by: test_r4_unaddressable_names_are_dropped_not_silently_indexed
KILLED               M6  index-based self-ref (R6)
                     killed by: test_r6_self_reference_survives_its_ref_key_being_barred_from_the_index
KILLED               M7  ambiguous=all exclusions (R7)
                     killed by: test_p2_2_duplicate_id_keeps_neither_reverse_index_row,
                                test_r7_excluded_ref_keys_and_ambiguous_ref_keys_are_different_sets
KILLED               M8  drop capability-ambiguous arm (R5)
                     killed by: test_r5_reference_to_an_excluded_target_is_not_reported_as_not_found
KILLED               M9  drop inventory schema gate (R8)
                     killed by: test_r8_garbage_inventory_file_is_schema_invalid_not_a_healthy_zero
KILLED (hung >180s)  M10 drop O_NONBLOCK prev-catalog (R2)
                     killed by: the suite itself wedges -- exactly the defect
KILLED               M11 drop O_NONBLOCK read_one_source (R2b)
                     killed by: test_r2_read_one_source_open_requests_o_nonblock
KILLED               M12 BaseException -> Exception (R14)
                     killed by: test_r14_keyboard_interrupt_is_reported_then_reraised
KILLED               M13 remove the --output pin (R9)
                     killed by: test_default_dir_itself_and_descendants_are_accepted,
                                test_dotdot_escape_from_pinned_root_rejected_post_resolve,
                                test_no_output_override_flag_exists_at_all,
                                test_rejects_output_outside_the_real_default_dir, (+1)
KILLED               M14 drop ambiguous_page_global_ids (R1b)
                     killed by: test_b1_b_duplicate_page_ids_are_flagged_and_retained_not_dropped
KILLED               M15 self-ref target_global_id from index (R6)
                     killed by: test_r6_self_reference_survives_its_ref_key_being_barred_from_the_index
```

The run above used the test file as it stood a few minutes before the final edit (a robustness tweak inside
`test_no_output_override_flag_exists_at_all`, swapping an argparse-private walk for the public
`parse_args(['build'])` + `hasattr` check). **M13 was therefore re-run against the final file and is still
KILLED**, by 5 tests including that one. No other mutant touches the tweaked test.

M10 is worth reading twice: reverting that one flag does not fail the suite, it **wedges** it — the run had to be killed at 180 s. That is the defect reproducing itself inside the harness.

### 7. Fresh live-fleet build — `build --force --json`

**Snapshot only. Re-derive at review time.**

```
exit 0 · rebuilt: true · elapsed 0.662s · degraded_count 0 · degraded []
repos 27 · worktrees 143 (totalCount 143, truncated false) · scan_targets 146
project_status_histogram: {path-missing: 1, no-wiki-dir: 138, ok: 7}
wiki_projects 7 · wiki_pages 42 · wiki_links 62
inventory_projects 1 · inventory_commands 235
capability_projects 2 · capabilities 16
depends_on_refs 7 {resolved 5, unresolved 2} · self_reference_refs 0

NEW fields, all zero on a clean fleet (as expected — the fleet contains none of these shapes):
  duplicate_depends_on_entries                 0
  duplicate_edges                              0
  excluded_ref_keys                            0
  ambiguous_page_global_ids                    0
  capabilities_with_unaddressable_project_id   0
new top-level keys: ['excluded_ref_keys', 'ambiguous_page_global_ids']
```

**No live degradation from the new validation** — all 16 capabilities and all 42 pages survive, byte-for-byte the same identities as before the change:
```
cap global_ids unchanged vs. pre-change catalog:  True
page global_ids unchanged vs. pre-change catalog: True
pages dup-flagged: 0 · caps with unaddressable project_id: 0
```

**Invariants (these are what a reviewer should re-check, not the counts):**
```
resolved + unresolved + self_reference == depends_on_refs      True
in_degree == len(referenced_by) for every reverse row          True   (new, R3)
every capability_ref_index value has a reverse row             True   (Judgment A)
ambiguous_ref_keys disjoint from capability_ref_index          True
excluded_ref_keys disjoint from capability_ref_index           True   (new, R7)
page & capability global_id namespaces disjoint                True   (now enforced, R1a)
each source_status_histogram sums to scan_targets              True   (B2)
```

The catalog's own provenance field agrees with the shipped file:
```
catalog generator.sha256 == 826d558882c57e7fd316e6cb4f909f1ce54ded850033708a54e2c4adeab99834
matches the on-disk build_cross_project_catalog.py: True
```

### 8. Pilot files — byte-identical and still clean
```
7de6af0f…d875d18  完善orca/wiki/reusable-capabilities.json
d08fb975…3aad398  projects/rn邮箱/wiki/reusable-capabilities.json

{"path": "wiki/reusable-capabilities.json", "ok": true, "project": "orca/完善orca", "capability_count": 11, "errors": [], "warnings": []}
{"path": ".../rn邮箱/wiki/reusable-capabilities.json", "ok": true, "project": "rn邮箱", "capability_count": 5, "errors": [], "warnings": []}
```

### 9. Nothing committed, nothing deployed
```
HEAD 8f35de0963c9f6324ccf39568ad8c1b488ce6370   (moved from 223ace9000 by other sessions)
git diff --cached                                (empty)
?? orca-context-bridge/scripts/build_cross_project_catalog.py
?? orca-context-bridge/scripts/test_build_cross_project_catalog.py
~/.agents/skills/orca-context-bridge/scripts/    -> no build_cross_project_catalog.py present
/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/ -> catalog.json only, no stale lock
```

---

## Notes for the next reviewer

1. **Fleet counts move.** See the banner at the top. Check the *invariants* in §7, not the numbers.
2. **Two judgment calls were deliberately preserved, not silently changed.** Judgment A (barring a duplicated global_id's ref_key) survives the R7 split unchanged and was re-proved load-bearing by re-running the KeyError counterfactual (§5). Judgment B (`ok+partial` contributing counts) is unchanged; R10 fixed only its stated *rationale*, which was false in the degenerate all-dropped case.
3. **Two deliberate scope decisions**, both stated inline in the code so they can be argued with:
   - The genuinely-duplicated-**ref_key** case keeps reporting `reason: "capability-not-found"` (per the fix brief), *not* the new `capability-ambiguous` — because "which of the two did you mean?" has no answer at all, unlike the excluded cases. It reports itself via `ambiguous_ref_keys[]`, and the new `target_excluded_reason` field carries the precise axis for **both** shapes, so neither is a silent misdiagnosis.
   - A duplicated `depends_on` entry is deduped and **counted**, but does **not** degrade its source to `partial`. Dedup is lossless (the resulting graph is what the author meant); a dropped malformed entry is not. Recorded per-capability and fleet-wide instead of made invisible.
4. **One review finding was verified false and handled differently than hypothesised:** `project_id` is *not* structurally safe (R4's parenthetical), but excluding on it would have broken working same-project resolution. Recorded via `project_id_addressable` rather than enforced. Reasoning is in the code comment at the key-assignment pass.
5. **Two extra items from the review's own P4 list are NOT in this round's brief** and were left alone, listed here so they are not mistaken for oversights: the review's own R11 (`projects_with_sources` vs. the `"contributed sources"` wording in `_print_human_summary`) and its own R14 (`redundant_spelling` present on `resolved`/`self-reference` dep entries but absent on `unresolved`). The fix brief renumbered R11–R14 to a different set of items; both of the above are cosmetic and unaddressed.
