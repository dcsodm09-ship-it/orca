# CLAUDE `opus`/`max` — independent read-only review, M4 round 3

**Candidate:** `orca-context-bridge/scripts/build_cross_project_catalog.py` (+ its test suite and `SKILL.md`)
**Date:** 2026-08-22
**Method:** two independent parallel review lenses (Lens A: F1/F2 focus; Lens B: F3/F4/F6 + fresh hunt), reconciled by a third pass that **personally re-derived every surviving finding and every disputed severity**. The fix agent's own report was treated as an unverified claim throughout.
**Predecessor:** `CLAUDE-OPUS-MAX-REVIEW-m4-round2-2026-08-22.md` (P1=1, P3=5, P4=6, verdict NO-GO)

---

## Headline verdict

> ## P0 = 0 · P1 = 0 · P2 = 0 · **P3 = 4** · **P4 = 5**
>
> ### **GO to the Codex `sol`+`max` final gate — conditional on one documentation-only commit.**
>
> **The code is done.** Round 2's one blocking finding (F1) is genuinely closed, and I verified its acceptance criterion independently and more aggressively than either lens: the real-pilot classes stay green under **+1, +50, and +500** synthetic content growth on every axis, in both pilot projects. F2, F4, R11 and R14 are all mutation-killed. There is **no reproducible P0/P1**, which is the gate criterion in CLAUDE.md rule 4.
>
> **All four P3s are prose, not behaviour** — they change no byte of catalog output. But **P3-1 is round 2's F2 surviving at a second site in the same file**, and closing F2 was a *blocking* requirement of this round. Sending a file that contradicts itself about `global_id` disjointness — the M5 join key — into a final sign-off is the single most predictable way to get the whole F2 thread reopened by the gate reviewer.
>
> **Recommended sequence:** one docs-only commit fixing P3-1, P3-2 and P3-3 (≈6 sentences total, zero code paths touched), then dispatch the `[强制双复核]` Codex gate against the resulting candidate.

**Is a substantive round 4 likely? No.** The convergence is monotone in both count and severity — 16 findings / 3×P2 → 12 findings / 1×P1 → 9 findings / **0×P1, 0×P2** — and this round is the first in which neither lens, nor my own independent re-derivation, could construct any runtime defect at all. Every remaining item is a comment, a doc cell, a report label, or a deliberate documented trade-off. A pass that only edits prose is pre-gate cleanup, not a fourth iteration round.

---

## Integrity gate — measured by me at the start AND the end of this review, identical both times

| Artifact | sha256 | Verdict |
|---|---|---|
| **Trust anchor** `~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py` | `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` | **UNCHANGED** — exact match to the required value |
| `build_cross_project_catalog.py` (2075 lines) | `48ed75faf36ac0fb9fbf93f6a5da668e3c0c542dfe426ac7c8697d4342ff671d` | untracked (`??`), unchanged start→end |
| `test_build_cross_project_catalog.py` (2641 lines) | `14140eda42588454f1bb61496744462d7d1e5f2e8834360a2bd1241b17a6598c` | untracked (`??`), unchanged start→end |
| `orca-context-bridge/SKILL.md` (917 lines) | `94065d6f701b11c286543f9738d827f9929ed0e418dd0375901674eefbb7f390` | `M` in worktree, **not staged** |
| Pilot `完善orca/wiki/reusable-capabilities.json` | `22a9722cf54be2743dec2f5c1d7426d0fa6989341a9aa80b627077ef1b31cbb2` | untouched by this round |
| Pilot `rn邮箱/wiki/reusable-capabilities.json` | `d08fb975e71dce0f7724f1c017bd0c5672b2ecbe67d9a3dad7b1ae8473aad398` | untouched by this round |
| Production `catalog.json` | `3f4ba566c06c731fa0f4ef30f7579310eee9f23585ef7777aac095a6be840530` | untouched; `generator.sha256 = 826d5588…` (**round-2** bytes) |

`HEAD = c756c85dbc61097d3926b5179de82563a3e21cdb`. Nothing staged, nothing committed, **nothing deployed** — `find ~/.agents/skills -name "*cross_project_catalog*"` → empty.

Both lenses independently reported these same seven hashes, and my own measurements match all seven exactly.

### Suite state — green against the pilot files *as they exist right now*

```
$ cd orca-context-bridge/scripts && python3 -m unittest test_build_cross_project_catalog
Ran 97 tests in 10.975s
OK
```

Verified non-vacuous by me: **97 `... ok`, 0 skipped**, and all **7** `RealWanshanOrcaPilotTests` / `RealRnMailPilotTests` methods actually ran (confirmed by name in `-v` output). 97 `def test_` methods = 97 tests executed; no `@unittest.skip` / `@expectedFailure` anywhere.

---

## What is genuinely fixed — re-derived first-hand, do not re-raise

| Round-2 finding | My independent evidence |
|---|---|
| **F1 (P1, the blocking one)** — suite frozen to live pilot content | **Acceptance criterion met, verified harder than either lens.** I cloned both real `wiki/` trees to scratch, normalised declared paths, and grew *every* axis (capabilities, wiki pages, `content_version`, CLI commands) in *both* projects: `+1 → PASS 7/7`, `+50 → PASS 7/7`, `+500 → PASS 7/7`. Every numeric pin round 2 named is now derived from an independent re-read of the file rather than a literal. |
| **F2 (P3)** — "namespaces are genuinely disjoint" was a false invariant | Collision measured, not promised. I rebuilt the counter-example end-to-end through the real CLI (below) and confirmed it is **published**, with `degraded_sources 0` and exit `0` (pure reporting). Mutant forcing the list to `[]` → **KILL**. |
| **F4 (P3)** — a regression would hang the suite forever | **Strongest result of the round.** Dropping `os.O_NONBLOCK` from `_read_previous_catalog` in a scratch copy: the suite **finishes in 31.5 s with `FAILED (failures=2)`** (`test_r2_a_o_nonblock…`, `test_r2_b_fifo…`). Round 2 measured *HANG, never finishes, SIGKILL required*. |
| **F5 → R11** — `projects_with_sources` wrong and untested | Mutant `projects_with_sources += 0` → **KILL** (2 failures: `test_r11_a_partial_source_still_counts_as_contributing`, `test_r11_projects_with_sources_excludes_projects_that_contributed_nothing`). |
| **F5 → R14** — `redundant_spelling` missing on the unresolved arm | Mutant removing it from the unresolved arm (3rd site, `:1462`) → **KILL** (`test_redundant_spelling_is_attached_on_all_three_parsed_arms`). |
| **F6** — production catalog written by unapproved code | **This round did not touch it**, proven two independent ways (below). |
| **Round-2 F10** — `assertIn(code, (0,1))` | 0 occurrences remain. |
| **Round-2 F12** — `O_NOFOLLOW` at `atomic_write_within` untested | Mutant dropping it → now **KILL** in 11.6 s (`test_f9_tmp_file_open_flags_include_o_nofollow_and_o_excl`); it survived round 2's suite. |
| **Round-2 F9** — self-reference/unresolved refs not deduped across spellings | **Still open, and honestly disclosed** by the fix report as an explicit deferral. Both lenses re-derived round 2's exact repro. Correctly not counted as a new finding. |

### F2 counter-example, re-derived by me end-to-end through the real CLI

Two fixture projects: a directory literally named `foo#page:x` holding capability id `bar`, and a directory `foo` holding wiki page id `x#bar`.

```
exit code            : 0
page gids (filtered) : ['foo#page:x#bar']
cap  gids (filtered) : ['foo#page:x#bar']
independent OVERLAP  : ['foo#page:x#bar']
published            : ['foo#page:x#bar']
counts               : 1
degraded_sources     : 0
addressable          : [('foo#page:x#bar', False)]
ID_RE.match('bar')   : True   <-- the colliding capability id is ID_RE-CLEAN
```

That last line is the load-bearing one, and it is what drives P3-1 below.

### F6 — production `catalog.json` untouched by this round (two independent lines of evidence)

1. **Timeline, from mtimes I read myself.** The round-2 review is stamped `18:03:45`, so the fix round cannot have begun earlier. The candidate files were written `18:11:45` / `18:18:24` / `18:22:43`, the fix report `18:27:42`. The production catalog's mtime is `17:38:12` — **25 minutes before the earliest possible start** — and both pilot files predate it too (`17:38:08`, `13:21:07`).
2. **Provenance, independent of any clock.** Its `generator.sha256` is `826d5588…`, the **round-2** candidate; this round's bytes are `48ed75fa…`. The file cannot have been produced by the code under review, and no deployed copy exists that could have written it.

No code change was expected here and none was made — **correct**. The *coordination* item round 2 raised remains open for a human after the gate: the live production catalog carries a generator hash matching neither the approved tree nor the current candidate. That is not a defect in this candidate.

---

## Reconciliation of the two lenses

| # | Finding | Lens A | Lens B | My adjudication |
|---|---|---|---|---|
| P3-1 | 2nd "genuinely disjoint" overclaim at `:469-472` | **missed** | P3 | **CONFIRMED P3** — re-derived with an ID_RE-clean id |
| P3-2 | SKILL.md `capability-not-found` false for `duplicate-ref-key` | **missed** | P3 (self-flagged as borderline) | **CONFIRMED P3** — target has the capability *twice* |
| P3-3 | Fix report renumbered by 3; headline false | P4 (A6) | P3 | **P3** — sided with Lens B, see rationale |
| P3-4 | Skip-guard comment claims "PRESENT AND" | P3 (A1) | disputed ("careful enough") | **CONFIRMED P3** — Lens A is right; absence → RED, not skip |
| P4-1 | Residual shape pins (`:757`, `:809`, `:812`) | 2 findings (A2, A3) | 1 finding (P4-1) | **Merged into 1 × P4** (3 sites, one family) |
| P4-2 | `_EXCLUSION_PRIORITY` merge branch unreachable | missed | P4 | **CONFIRMED P4** — proven 3 ways |
| P4-3 | 2 surviving mutants (lockfile `O_NOFOLLOW`, `sorted()`) | missed | P4 | **CONFIRMED P4** — reproduced both |
| P4-4 | 3 baseline floors at zero headroom | P4 (A4) | missed | **CONFIRMED P4** — measured exactly |
| P4-5 | `setUp` / `process_target` double-read window | P4 (A5) | missed | **CONFIRMED P4** — structural, self-healing |

**Dedup note:** Lens A's A2 + A3 and Lens B's P4-1 describe the same defect family at overlapping sites; merged into one P4 covering all three lines. Lens A filed 6 findings, Lens B filed 6; the union after dedup is **9**.

**Each lens found real things the other missed.** Lens B caught the two substantive prose defects (P3-1, P3-2) that carry the actual gate risk; Lens A caught the two test-side items (P4-4, P4-5) and was right on the disputed P3-4. Neither lens alone would have produced this list.

---

## Findings

### P0 — none · P1 — none · P2 — none

I attempted to construct a P1/P2 and could not. Every finding below is documentary or latent, and none alters catalog output.

---

### P3-1 — the "genuinely disjoint" overclaim F2 was raised about **survives at a second site**, and the file now contradicts itself

**`build_cross_project_catalog.py:469-472`**, contradicted by the same file at `:613-625` and by this round's own new code at `:1243-1245`.

```
469:        #      page's -- silently, since the duplicate-detection pass below
470:        #      only ever compares capability global_ids against each other.
471:        #      ID_RE forbids ':', which is what makes the two namespaces
472:        #      genuinely disjoint rather than disjoint-by-assumption.
```

The fix agent corrected the **page-mint site** (`:603-645`), which now says plainly:

> *"The `project_id` axis is NOT enforced, and this comment used to overclaim that no collision 'can ever be constructed'. It can."*

…but left this **second, present-tense assertion of the identical claim** 140 lines earlier. Note that the corrected site carefully scopes its claim to *"along the `id` axis … that half is ENFORCED"*; `:471-472` carries no such qualifier and asserts disjointness flatly.

**Repro (personally run, end-to-end through the real CLI — full output above):** project dir `foo#page:x` + capability id `bar`; project dir `foo` + page id `x#bar` → both mint `foo#page:x#bar`. **`ID_RE.match('bar')` is `True`** — the colliding id is perfectly ID_RE-clean, so ID_RE is demonstrably *not* "what makes the two namespaces genuinely disjoint." The collision arrives through the unenforced `project_id` axis.

**Honest narrowing of Lens B's secondary point.** Lens B also called `:469`'s word *"silently"* falsified. Partially: the *net* behaviour is no longer silent (`:1243-1245` publishes the overlap), but the justification the sentence actually gives — *"the duplicate-detection pass below only ever compares capability global_ids against each other"* — remains literally true, since the cross-namespace intersection is a separate pass. The overclaim in `:471-472` is the confirmed defect; "silently" is a weaker contributing inaccuracy.

**Why P3, and why it should be fixed before the gate:** round 2 rated the identical claim P3, and closing it was a **blocking** requirement of this round. Rounds 1 and 2 both produced NO-GO verdicts driven by false invariants about `global_id` — the field whose entire purpose is to be M5's join key. **Fix:** rewrite two sentences at `:469-472` to defer to `:613-625` rather than restating the claim.

---

### P3-2 — SKILL.md's `capability-not-found` row is false for the `duplicate-ref-key` carve-out, and the carve-out is undocumented

**`orca-context-bridge/SKILL.md:821`**, versus code at `build_cross_project_catalog.py:1477` and its own rationale comment at `:1486-1492`.

```
| `capability-not-found` | `in-catalog-with-capabilities` | the target project has capabilities, but not that one |
```

The code deliberately carves `duplicate-ref-key` **out** of `capability-ambiguous` (`:1477`: `elif excluded_reason is not None and excluded_reason != "duplicate-ref-key":`), so a reference to a genuinely-duplicated ref_key falls through to `capability-not-found`. The target project **has** that capability — twice.

**Repro (personally run):** project `dupref` with two capabilities both resolving to ref_key `dupref:script:same.py`; project `srcp` referencing it.

```
reason                : capability-not-found
target_project_state  : in-catalog-with-capabilities
target_excluded_reason: duplicate-ref-key
ambiguous_ref_keys    : ['dupref:script:same.py']
REALITY: dupref capabilities in catalog: [('one','dupref:script:same.py'), ('two','dupref:script:same.py')]
```

The documented Meaning — *"but not that one"* — is affirmatively **false** here, not merely incomplete. `duplicate-ref-key` appears in SKILL.md exactly once (`:828`) as a possible `target_excluded_reason` value, and nothing tells a consumer that this third exclusion axis lands in `capability-not-found` rather than `capability-ambiguous`.

**Honest caveat (Lens B self-flagged this as its weakest P3, and I agree it is borderline):** no data is lost — `target_excluded_reason` and `ambiguous_ref_keys[]` both carry the truth on the same row — so the harm is a misdiagnosis detour for a human reader, not a broken consumer. A reconciler could defensibly rate this P4. I hold it at P3 because it is a false cell in the **published consumer contract table this very round wrote**, and eliminating exactly that class of defect was F3's brief. **Fix:** one clause carrying the code's own `:1486-1492` rationale into the table.

---

### P3-3 — the fix report's `F7–F12` table is renumbered by three, making its headline false on two counts

**`M4-CANDIDATE-round3-fixes-2026-08-22.md:10-13`** (headline) and `:309-315` (table).

The report's F1–F6 align with round 2. From F7 on they silently shift by three:

| Report row | Actually round 2's |
|---|---|
| "F7 — 10 × `assertIn(exit_code,(0,1))`" | **F10** |
| "F8 — `ID_RE.match()` trailing `\n`" | **F11** |
| "F9 — `O_NOFOLLOW` at `atomic_write_within` untested" | **F12** |
| "F10 / F11 — opportunistic test hygiene" | *not a round-2 ID at all* |

Read against round 2's numbering — which is what *"the six optional ones"* refers to, and what a gate reviewer will apply, having just seen F1–F6 align — the headline

> *"Three of the six optional ones (**F7, F9**, plus test hygiene under F10/F11) are done. One (**F8**) is deliberately NOT changed"*

asserts that **round-2 F9 is done** (it is not — the report itself says so five lines later) and that **round-2 F8 is unchanged** (it *was* changed; I mutation-verified `redundant_spelling` is now on all three arms). The same table uses "F9" for two different findings: row 3 (`O_NOFOLLOW`) and the final row, explicitly labelled "round-2 **F9**". This sits under a section header reading *"Headline — stated precisely, so it cannot contradict its own footnotes."*

**Repro:** diff `M4-CANDIDATE-round3-fixes-2026-08-22.md:309-315` row labels against `CLAUDE-OPUS-MAX-REVIEW-m4-round2-2026-08-22.md:151-156`.

**Severity — I sided with Lens B (P3) over Lens A (P4).** Lens A's counterpoint is substantially true and is why this is not P2: I verified independently that **no actual work is misrepresented** — every item is genuinely done or genuinely deferred, and the deferral paragraph is accurate and prominent. But round 2 filed the *identical* defect class as P3 (its F5, renumbering), the report is the primary input the final gate consumes, and a reader who trusts the headline concludes the wrong thing about what is deferred. Mitigating and worth recording: the correcting paragraph is only five lines below the headline, within the same screenful.

---

### P3-4 — the skip-guard comment overstates the guard: absence is *not* guarded, and yields a RED suite rather than a skip

**`test_build_cross_project_catalog.py:600-604`**, versus the guard implementation at `:655-659`.

```
600: # The skip guards check that the pilot files are PRESENT AND
601: # INDEPENDENTLY PARSEABLE, not merely that a directory exists ...
```

The "PRESENT" half is false. At `:657-659` a missing file sets `self.raw[name] = None` and `continue`s — no skip. What the guard actually delivers is *"the project directory exists, and any file that **is** present is parseable."*

**Repro (personally run).** I cloned both real `wiki/` trees to scratch, normalised the declared paths so the control is clean, applied one mutation per case, and re-pointed both pilot classes:

```
0 control                        run=7 fail=0 err=0 skip=0  -> GREEN
1 project dir absent             run=7 fail=0 err=0 skip=3  -> SKIPPED (hermetic)   OK
4 caps FILE unparseable          run=7 fail=0 err=0 skip=3  -> SKIPPED (hermetic)   OK  <- the new widening works
2 wiki/ dir absent               run=7 fail=3 err=0 skip=0  -> RED  "unexpectedly None"
3 caps FILE absent               run=7 fail=1 err=0 skip=0  -> RED  "{'status': 'absent'}"
6 wanshan cli-inv absent         run=7 fail=1 err=0 skip=0  -> RED  "{'status': 'absent'}"
```

**Adjudicating the lens disagreement:** Lens B characterised this comment as *"careful enough not to re-assert"* the hermeticity claim. That is true of the **hermeticity** sentence specifically, but Lens A's finding is about the word **"PRESENT"**, which is present in the comment and is empirically false. Lens A is correct.

**Non-blocking:** unreachable on the gate machine — both trees are present, and `wiki/` is git-tracked as of `c756c85dbc`. Rated P3 by round 2's own calibration (its F4, a test docstring falsely claiming a guard it structurally could not deliver, was P3), and this prose was written *this* round inside the comment block that replaced a claim round 2 flagged as false. **Fix:** delete two words, or add `self.raw[name] is None → skipTest` at `:658`.

---

### P4-1 — three residual shape pins survived F1's decoupling (merged from Lens A's A2+A3 and Lens B's P4-1)

**`test_build_cross_project_catalog.py:757`, `:809`, `:812`.**

F1 decoupled the suite from content **magnitude** (verified: +500 growth stays green). It did not decouple it from content **shape** — a file appearing or disappearing, or a declared path changing:

| Line | Pin | Repro (personally run) → result |
|---|---|---|
| `:809` | `assertTrue(entry["declared_path_matches"])` | **Fires merely by relocating the tree.** My very first control run against an unmodified clone went RED here before I normalised paths: `AssertionError: False is not true` |
| `:812` | `assertEqual(sources["orca-cli-capability-inventory.json"]["status"], "absent")` | rn邮箱 **adopts** a CLI inventory → `RED, fail=1, test_absent_cli_inventory_is_absent_not_an_error` |
| `:757` | `assertIsInstance(entry["declared_path"], str)` | 完善orca wiki **drops** `project.path` → `RED, fail=1, "None is not an instance of <class 'str'>"` |

The `:809` trigger is demonstrably real on this fleet: the sibling project 完善orca **already carries exactly this drift** (its wiki still names a pre-SSD-cutover `/Users/...` path), which is why the same field was deliberately un-frozen for it at `:753-757`. Not rated higher because `:808` documents the choice explicitly, the base helper at `:726-733` correctly asserts the *relation*, and a failure would be a true statement about the data.

Related and worth recording: the fix report's `:314` claim that the widened guard makes *"the docstring's hermeticity claim true for the first time"* is **not supported** — the class still fails rather than skips on a differently-shaped fleet. The delivered code comment is more careful than the report.

---

### P4-2 — `_EXCLUSION_PRIORITY`'s merge branch is unreachable, and its comment credits a mechanism that never runs

**`build_cross_project_catalog.py:1276`, `:1308-1313`; comment `:1272-1274`.**

> *"Highest-priority reason wins so the reported reason is stable no matter what order duplicates appear in."*

The stability is real, but it comes from the deterministic per-capability `if ref_dup / elif gid_dup / else` chain at `:1298-1303`, **not** from the merge.

**Proven unreachable three independent ways:**

1. **Analytical, now airtight.** `duplicate_ref_keys = {k for k, n in ref_key_counts.items() if n > 1}` (`:1225`) is computed **globally over all capabilities**. `excluded_ref_key_reasons` is keyed by `ref_key`, so `previous is not None` requires two capabilities to *share* a ref_key — which forces that key's count `> 1`, hence `ref_dup = True` for **both**, hence both compute `reason = "duplicate-ref-key"` (priority index 0). `0 < 0` is `False` by construction.
2. **Instrumentation (personally run).** I patched the branch to log every entry and ran the full 97-test suite: **3 hits, all `prev=duplicate-ref-key new=duplicate-ref-key WOULD-SWAP=False`.**
3. **Mutation (personally run).** **Reversing `_EXCLUSION_PRIORITY` entirely SURVIVES the whole suite** — because the mutant is semantically inert, not because coverage is thin.

Benign (no output is ever wrong), but it is ~10 lines of unreachable code plus a comment asserting a mechanism that does not exist — the same "comments must not overclaim" family this round exists to clean up.

---

### P4-3 — two mutants still survive: the lockfile's `O_NOFOLLOW`, and `sorted()` on the new collision list

**`build_cross_project_catalog.py:1805`** (lockfile open) and **`:1243`** (`sorted(...)`). My own mutation battery:

```
[KILL    ] drop O_NOFOLLOW @1772 tmp open (round-2 F12)   11.6s  test_f9_tmp_file_open_flags_include_o_nofollow_and_o_excl
[SURVIVED] drop O_NOFOLLOW @1805 LOCKFILE open            12.0s
[SURVIVED] drop sorted() on cross-namespace collisions    11.9s
[KILL    ] force cross_namespace collisions to []         11.9s  test_f2_cross_namespace_global_id_collision_is_measured_and_published
```

Round 2's F12 flagged `O_NOFOLLOW` at `atomic_write_within` as unpinned; this round closed **that** site but not its direct twin at `:1805`. Both survivors are benign: `O_CREAT|O_EXCL` already raises `EEXIST` on a symlink regardless of `O_NOFOLLOW` (round 2 established this POSIX reasoning), and the real code *does* have `sorted()`, so no live output is affected. Filed only so the "all four opens" property does not stay half-pinned, and because without `sorted()` string-hash randomisation would make the catalog bytes flap across processes.

---

### P4-4 — three `_BASELINE_*` floors sit exactly at current live values, so the suite is red on any deletion

**`test_build_cross_project_catalog.py:618-623`.** Measured by me against the live files:

| Floor constant | floor | live | headroom |
|---|---|---|---|
| `_BASELINE_WANSHAN_CAPABILITY_COUNT` | 11 | 12 | 1 |
| `_BASELINE_WANSHAN_CONTENT_VERSION` | 1 | 2 | 1 |
| `_BASELINE_WANSHAN_PAGE_COUNT` | 1 | 15 | 14 |
| **`_BASELINE_WANSHAN_COMMAND_COUNT`** | **235** | **235** | **0** |
| **`_BASELINE_RN_MAIL_CAPABILITY_COUNT`** | **5** | **5** | **0** |
| **`_BASELINE_RN_MAIL_PAGE_COUNT`** | **3** | **3** | **0** |

Any single deletion on those three axes turns the suite red. The anti-vacuity rationale is documented at `:594-597` and growth is unbounded in the safe direction, so this is a deliberate one-sided trade, not a defect — recorded because every pin was audited.

---

### P4-5 — `setUp`'s independent read and `process_target`'s read are two separate snapshots

**`test_build_cross_project_catalog.py:655-667`** (independent read) versus **`:668`** (`process_target`, which re-reads). A concurrent write landing between them makes the two disagree, producing a spurious count mismatch.

Confirmed structurally by inspection: `setUp` parses each allow-listed file itself, then calls `bcpc.process_target(...)`, which opens the same files again. Sub-millisecond window, self-heals on re-run, and `:663-667` already skips the mid-write-*unparseable* case — strictly better than round 2's permanent-red behaviour. Recorded for completeness only; this is inherent to the "assert against an independent re-read" design, which is itself the correct fix for F1.

---

## Recommendation

**GO to the Codex `sol`+`max` `[强制双复核]` final gate, after one documentation-only commit.**

The gate criterion in CLAUDE.md rule 4 — *"不得发现可复现 P0/P1"* — is satisfied: **P0 = 0, P1 = 0, P2 = 0**, across two independent lenses plus my own re-derivation of every claim. The code needs no further change.

**Fix first (all prose, ~6 sentences, zero code paths touched):**

1. **P3-1** — `build_cross_project_catalog.py:469-472`: drop the "genuinely disjoint" assertion; defer to `:613-625`. *This is the one that matters.* It is round 2's F2 at a second site, in a file that now contradicts itself about the M5 join key.
2. **P3-2** — `SKILL.md:821`: one clause noting the `duplicate-ref-key` carve-out lands here.
3. **P3-3** — `M4-CANDIDATE-round3-fixes-2026-08-22.md`: renumber the `F7–F12` table to round 2's IDs and correct the headline.

**Defer with recorded rationale:** P3-4 (unreachable on the gate machine) and all five P4s. Round-2 F9 remains a correctly-disclosed deferral.

**On the risk of a fourth round.** I do not expect one on code. Three signals support this: the finding count and severity fell monotonically (16/3×P2 → 12/1×P1 → 9/0×P1); this is the first round where the strongest mutants — `O_NONBLOCK`, `projects_with_sources`, `redundant_spelling`, the F2 collision list, `O_NOFOLLOW` at the tmp open — are *all* killed; and the F1 acceptance criterion held at 500× growth, far beyond what the brief demanded. The residual risk is concentrated entirely in P3-1: if it ships unfixed, the likeliest failure mode is not a new defect but the gate reviewer re-opening the settled F2 thread on seeing a self-contradicting file.

---

## Reviewer's disclosure

- **Nothing was modified, staged, committed, or deployed.** All seven tracked artifacts, including the trust anchor and production `catalog.json`, hash identically at the start and the end of this review. `git status` for the three files under review is unchanged (`??` / `??` / `M`); `git diff --cached` is empty; `HEAD` is still `c756c85dbc`.
- Every mutation, clone, growth test, instrumentation run and end-to-end fixture build ran **only** inside the session scratchpad (`…/6c489df8-…/scratchpad/recon/`), against copies. No build in this review wrote to `/Volumes/Extreme SSD/Orca/manifests/`.
- The one benign side effect is refreshed `__pycache__/*.pyc` mtimes under `orca-context-bridge/scripts/` from running the suite — gitignored (`.gitignore:1:__pycache__/`), the same effect rounds 1 and 2 documented.
- Concurrent-session activity: both pilot files were stable throughout this review. `HEAD` has moved since round 2's `8f35de0963` due to other sessions in this shared repo; no candidate file is affected. The pilot files' mtimes (`17:38:08`, `13:21:07`) both precede the fix round's earliest possible start (`18:03:45`), confirming **this** round did not touch them.
- The fix agent's own report was not trusted as evidence at any point; every claim credited above was independently re-derived.
