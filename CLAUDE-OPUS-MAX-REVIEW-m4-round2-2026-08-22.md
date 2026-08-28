# Reconciled independent review — M4 `build_cross_project_catalog.py`, round 2

**Reviewer:** Claude `opus` / `max`, read-only, reconciling two independent parallel lenses.
**Date:** 2026-08-22
**Candidate:**
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/build_cross_project_catalog.py`
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/test_build_cross_project_catalog.py`
**Supersedes for verdict purposes:** the fix agent's self-report `M4-CANDIDATE-round2-fixes-2026-08-22.md`.
**Round-1 review being answered:** `CLAUDE-OPUS-MAX-REVIEW-m4-catalog-builder-fixes-2026-08-22.md` (16 findings, R1–R16, P2×3 / P3×6 / P4×7, NO-GO).

Nothing in this review was taken on trust. Every repro below was re-derived from primary sources and driven first-hand by the reconciler, including all nine of R1–R9. Where the two lenses disagreed, the disagreement was settled by running the code, not by preferring a lens.

---

## HEADLINE VERDICT

> # NO-GO for the Codex `sol` + `max` final sign-off gate.
>
> ### P0 = 0 · **P1 = 1** · P2 = 0 · P3 = 5 · P4 = 6 — **12 findings after dedup.**
>
> **The production-code work of this round is genuinely good.** All sixteen of round 1's findings that were in the fix brief are correctly and completely fixed; I re-derived every original repro against the current bytes and could not make a single one fire again. There is **no P0 and no production-code P1 or P2** — a real improvement over round 1's three P2s.
>
> **What blocks the gate is the delivered test suite: it does not pass.** 85 of 86, deterministically, and it will not self-heal. Worse, the obvious one-line fix does **not** make it green — a second pin is already violated behind the first, which I proved by applying that fix in a staging copy. Until a final-gate reviewer can run this suite and see green, the gate cannot mean anything.

---

## Integrity gate — verified at start AND at end of this review

| Artifact | sha256 | Verdict |
|---|---|---|
| Trust anchor `~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py` | `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` | **unchanged** ✅ — matches the required value exactly, start and end |
| `build_cross_project_catalog.py` | `826d558882c57e7fd316e6cb4f909f1ce54ded850033708a54e2c4adeab99834` | matches the fix report's claim ✅, unchanged start → end (1994 lines) |
| `test_build_cross_project_catalog.py` | `75537dbbedc9f6a3c84b0c754dbbb1021a7f4710beffdb3abfb1a9483cd45c92` | matches the fix report's claim ✅, unchanged start → end (2221 lines) |
| Pilot `projects/rn邮箱/wiki/reusable-capabilities.json` | `d08fb975e71dce0f7724f1c017bd0c5672b2ecbe67d9a3dad7b1ae8473aad398` | **byte-identical** to the fix report's value ✅ — `{"ok": true, "errors": [], "warnings": []}`, 5 caps |
| Pilot `完善orca/wiki/reusable-capabilities.json` | `22a9722cf54be2743dec2f5c1d7426d0fa6989341a9aa80b627077ef1b31cbb2` | ⚠️ **changed** from the fix report's `7de6af0f…` — **by a concurrent session at 17:38:08, not by the fix agent and not by any reviewer.** Still `{"ok": true, "errors": [], "warnings": []}`, now **12** caps (a 12th, `prime-agent-verified-install-pipeline`, was added). Both lenses independently observed the same change. |

**Deployment / commit state, verified by me:**
- Both candidate files are still **untracked** (`?? orca-context-bridge/scripts/build_cross_project_catalog.py`, `?? …/test_build_cross_project_catalog.py`). Nothing committed.
- `build_cross_project_catalog.py` does **not** exist anywhere under `~/.agents/skills/` — **not deployed**.
- `/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/` contains only `catalog.json` — **no stale lock**.
- `HEAD = 8f35de0963c9f6324ccf39568ad8c1b488ce6370`.

**Environment drift that post-dates both lenses** (recorded so the next reviewer is not surprised): `orca-context-bridge/SKILL.md` and the six `wiki/*` files are now **staged** (`M`/`A` in the index) by a concurrent session, and `SKILL.md`'s mtime is now `17:47:11`. The fix report's "nothing staged" was true when written. Neither candidate file is affected. I re-verified finding **F3** below against SKILL.md's *current* bytes, not the version either lens read.

---

## Reconciliation summary — where the two lenses agreed, and where they did not

| Item | Lens 1 (R1–R9 correctness) | Lens 2 (fresh hunt + P4 audit) | **Reconciled ruling** |
|---|---|---|---|
| All of R1–R9 fixed | fixed, 10/10 | fixed (12/13 mutants killed) | **Agreed. I re-verified all nine first-hand.** |
| Suite is red | **P1** | **P2** | **P1** — see F1. New evidence I found (a second, masked pin) settles it upward. |
| Cross-project `global_id` collision still constructible | **P3** (raised) | asserted namespaces are "genuinely disjoint" (i.e. denied) | **Lens 1 is right; Lens 2 is wrong.** I reproduced the collision. **F2, P3.** |
| SKILL.md contract diverged | not raised | **P3** | **Confirmed, P3 — F3.** I re-reproduced against SKILL.md's current bytes. |
| R2 mutant hangs the suite; docstring false | observed the hang, filed no finding | **P3** | **Confirmed, P3 — F4.** |
| R11 / R14 unfixed while report claims all fixed | **P4** | **P3** | **Split:** the *misreporting* is **P3** (F5); the two underlying items stay at round 1's own **P4** (F7, F8). No double-counting of harm. |
| Production `catalog.json` written by candidate | disclosed as an observation | **P2** | **P3 — F6.** Downgraded: it is a derived, regenerable, explicitly-unpinned file, and I verified its current content is structurally sound. |
| Self-reference refs not deduped | **P4** (raised) | not raised | **Confirmed, P4 — F9.** |
| `assertIn(code, (0,1))` weakness | not raised | **P4** | **Confirmed, P4 — F10.** |
| `ID_RE.match` admits trailing `\n` | **P4** | **P4** | **Agreed, P4 — F11.** Both lenses independently reached the same (correct) "parity, not drift" conclusion. |
| `O_NOFOLLOW` at `atomic_write_within` untested | not raised | **P4** | **Confirmed, P4 — F12.** I verified it is unreachable-in-effect. |
| Lens 2's "P3-4 live-fleet blast radius" | — | filed separately | **Folded into F1** — Lens 2 itself said it is "not a separate defect". Not counted twice. |
| Lens 2's "P4-2 `projects_with_sources` untested" | — | filed separately | **Folded into F7** — same item. Not counted twice. |

---

## Findings

### P0 — none

### P1 (1)

#### F1 — The delivered test suite does not pass, and the obvious fix does not fix it

**File:line:** `test_build_cross_project_catalog.py:604` and `:613` (also latent: `:624`, `:626`, `:640`, `:642-651`, `:666`); false docstring at `:578-579`; class at `:586-626`.

`RealPilotFileTests` runs `process_target()` against **the two real project trees by absolute path** (`:582-583`) and then hard-pins exact scraped values:

```python
604:  self.assertEqual(caps_entry["capability_count"], 11)
613:  self.assertEqual(wiki_entry["content_version"], 1)
624:  self.assertEqual(inv_entry["command_count"], 235)
```

Those files are **untracked** (`wiki/` is not in git history for these paths in a way that would restore them) and are edited by other sessions. Both candidate files are byte-identical to the reviewed hashes throughout; only the environment moved.

**Repro A — the suite is red (I ran it twice; red twice; Lens 1 ran it 3×, Lens 2 8×; 13 red observations total):**
```
cd "/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts"
python3 -m unittest test_build_cross_project_catalog

  FAIL: test_real_wanshan_orca_pilot_eleven_capabilities
    File ".../test_build_cross_project_catalog.py", line 604
      self.assertEqual(caps_entry["capability_count"], 11)
  AssertionError: 12 != 11
  Ran 86 tests in 8.902s
  FAILED (failures=1)
```

**Repro B — the naive one-line fix leaves it red. This is the part neither lens demonstrated, and it is why this is P1 and not a cosmetic pin refresh.** I copied the suite to a scratch directory, changed only `11` → `12` at `:604`, and ran it:
```
  FAIL: test_real_wanshan_orca_pilot_eleven_capabilities
    File ".../t604.py", line 613
      self.assertEqual(wiki_entry["content_version"], 1)
  AssertionError: 2 != 1
```
A second violated pin was sitting behind the first, masked by `assertEqual`'s fail-fast. Independently confirmed by reading the live values straight out of production code:
```
python3 -c "<import bcpc>; o = bcpc.process_target({'real_path': '…/完善orca', 'project_id': 'orca/完善orca'})"
  capability_count (test pins 11):  12
  content_version  (test pins  1):   2
  command_count    (test pins 235): 235     ← still matching, for now
  page_count (assertGreaterEqual 1): 15
```

**Why this is P1 and not P2:**

1. **It is the only present-tense failure in the whole round.** Every other finding here — and every one of round 1's sixteen — is latent. This one is firing, now, on the delivering machine.
2. **It will not self-heal and cannot be waited out.** `wiki/` is not reverted by anything.
3. **`:613` pins a monotonically-incrementing counter.** `meta.content_version` exists precisely to increment — that is this repo's own M2 wiki-freshness design. Pinning it with `assertEqual` guarantees breakage on the next legitimate wiki edit, forever. This is a design defect in a delivered file, not drift.
4. **It falsifies prose written in this very round.** `:578-579` states the class is *"skipUnless-guarded so the suite stays hermetic (and still fully green) on a machine that does not have this exact fleet checked out."* `skipUnless` at `:592`/`:628` guards **directory presence** only, never **content** — so a machine that *does* have the fleet, with any different content, **fails rather than skips**. Round 1's NO-GO turned on exactly this class: false prose written in the round being reviewed.
5. **It makes every future review result uninterpretable.** During this review alone the fleet moved twice under the reviewers (`wiki_pages` 42→43, `capabilities` 16→17). A final-gate reviewer cannot distinguish environment drift from a real regression. *(This absorbs Lens 2's separately-filed "P3-4 live-fleet blast radius", which Lens 2 itself declined to count as a distinct defect.)*

**Honest caveat, in the fix agent's favour:** its claimed 86/86 was true when it ran at 17:26. The break came at 17:33:26 (`orca-context-wiki.json` `content_version` 1→2) and 17:38:08 (a 12th capability added), both by a concurrent session. The candidate did not regress. That excuses the *report*; it does not excuse the *design*, and it does not unblock the gate.

**Fix.** Make `RealPilotFileTests` insensitive to live content — the same test already demonstrates the resilient idiom two lines below the break, at `:620` (`assertGreaterEqual(page_count, 1)`). Pick one:
- copy the two pilot files into fixtures and point the test at the fixtures (best — makes it genuinely hermetic, which is what the docstring already promises); or
- derive the expected counts from the file at test time and assert structure, not magnitude; or
- pin the pilot files' sha256 and `skipUnless` the hash matches — which would make the docstring's "hermetic (and still fully green)" claim true for the first time.

Whichever is chosen, **audit all four pins, not one**: `:604`, `:613`, `:624`/`:626`, and the rn邮箱 set at `:640`/`:642-651`/`:666`. Then run the suite to green before re-submitting.

### P2 — none

Neither lens found a P2-grade production-code defect, and I could not construct one. Lens 1 filed zero. Lens 2's two P2s reconcile to **P1 (F1)** and **P3 (F6)** respectively. Given round 1 shipped three P2s, this is a genuine and creditable improvement.

### P3 (5)

| ID | File:line | Finding | Repro (personally run) |
|---|---|---|---|
| **F2** | `build_cross_project_catalog.py:588-598` (the rewritten comment), contradicted by the module's own comment at `:1157-1171`; mint sites `:608` (pages) and `:1153` (capabilities) | **The R1(c) disjointness claim is still falsifiable.** The new comment asserts disjointness is *"ENFORCED, not assumed"* and that **"no collision with a page global_id is constructible."** It is constructible. `ID_RE` closes the `id` axis; nothing closes the `project_id` axis — and the module's own comment 570 lines later says so explicitly: `project_id` "is not structurally safe either … a directory literally named with a ':'". **Lens 2 asserted the opposite ("genuinely disjoint"); I ran it, and Lens 1 is correct.** | Two real directories, real `build_scan_targets` + `assemble_catalog`: dir `foo#page:x` holding cap `{"id":"bar","kind":"skill","name":"s"}`; dir `foo` holding page `{"id":"x#bar"}`.<br>`page global_ids: ['foo#page:x#bar']`<br>`cap  global_ids: ['foo#page:x#bar']`<br>`OVERLAP        : ['foo#page:x#bar']`<br>`duplicate_page_global_id False · duplicate_global_id False · ambiguous_* [] · degraded 0`<br>`counts.capabilities_with_unaddressable_project_id: 1` ← a signal *does* fire, it is just not wired to the claim.<br>**Why P3, not P2:** it needs an adversarially-named *registered directory* (containing both `#` and `:`) plus a `#` in a page id — filesystem-level input, not file content; near-zero operational risk; live fleet clean. The fix R1(a) was actually asked for is complete. **Why not P4:** this is a false invariant, written this round, about `global_id` — the field whose entire purpose is to be M5's join key. That is the exact class that produced round 1's NO-GO, and the exact comment R1(c) was supposed to make true. **Fix:** one sentence — name the `project_id` precondition; or reuse the already-computed `project_id_addressable` (`:1172`) when minting global_ids. |
| **F3** | `orca-context-bridge/SKILL.md:810-827`, esp. **`:826`**; vs code `:1287`, `:1385`, `:1387`, `:1395`, **`:1412`**, `:1414` | **The published consumer contract now describes behaviour this round changed.** (a) SKILL.md `:826` states that for a shared `id` *"the ambiguous key is dropped from the lookup indices and recorded in `ambiguous_ref_keys` / `ambiguous_global_ids`"*. **After R7's split that is false for the ref_key half** — a consumer written to SKILL.md checks an array that is now always empty for this case. (b) SKILL.md enumerates a **closed list of four** unresolved reasons; the code emits **six** — `capability-ambiguous` (`:1412`) is new this round and undocumented. (c) Undocumented additions: top-level `excluded_ref_keys[]`, `ambiguous_page_global_ids[]`; per-row `duplicate_page_global_id`, `duplicate_edge`, `duplicate_depends_on_count`, `target_excluded_reason`, `project_id_addressable`; and `target_project_state: "in-catalog-target-excluded"`. **R7's split is correct — the doc must move with it.** | Project `pa` with two caps sharing `id:"x"` but distinct `(kind,name)`; project `pb` referencing `pa:script:alpha.py`:<br>`ambiguous_ref_keys : []`  ← SKILL.md says the dropped key is recorded *here*<br>`counts.ambiguous_ref_keys: 0`<br>`excluded_ref_keys  : [{"ref_key":"pa:script:alpha.py","reason":"duplicate-global-id"}, {"ref_key":"pa:skill:beta","reason":"duplicate-global-id"}]`<br>`ambiguous_global_ids: ['pa#x']`<br>`unresolved: pa:script:alpha.py | capability-ambiguous | in-catalog-target-excluded | target_excluded_reason: duplicate-global-id`<br>**Re-verified against SKILL.md's current bytes (mtime 17:47:11), i.e. after both lenses read it — the falsehood is still there.** |
| **F4** | `test_build_cross_project_catalog.py:2131-2156`; false docstring at **`:2144-2146`**; unreachable assertion at `:2152`; sibling guard at `:2158` | **An R2 regression HANGS the suite instead of failing it, and the test's own docstring claims the opposite.** `:2144-2146` says *"The generous 20s budget is a deadlock detector, not a perf assertion."* It cannot be: `self.assertLess(time.monotonic() - started, 20.0)` at `:2152` sits **after** `bcpc._read_previous_catalog(fifo)` at `:2151`, so it is unreachable exactly when the bug it claims to detect is present. The genuine guard is `test_r2_o_nonblock_is_actually_on_the_open_flags` (`:2158`), but both methods live in the same class `PreviousCatalogReadTests` (`:2094`) and unittest orders methods lexicographically — `test_r2_f…` < `test_r2_o…` — so the hang always precedes the guard and the guard never runs. `finally: fifo.unlink()` also never runs. | Structural, confirmed by reading `:2151-2152` and `:2094-2158`; and empirically corroborated by **both** lenses' independent mutation batteries, which each dropped `os.O_NONBLOCK` from `:1656` in a staged copy and reported the same outcome — **HANG, suite never finishes** — where every other mutant produced a clean KILL in seconds. CI would stall rather than report. **Fix:** run the call under a hard timeout (subprocess/alarm), and/or rename so the flag-spy sorts first. |
| **F5** | `M4-CANDIDATE-round2-fixes-2026-08-22.md:1`, `:5`, `:54`, `:59`, `:62`; self-correcting footnote at `:250` | **The fix report's headline is false, and it is the headline a gate reviewer reads.** `:1` — *"round 2: all 16 findings (R1–R16) fixed"*. `:5` — *"**every one of its 16 findings … is resolved in this candidate.** That review's NO-GO … no longer applies to these."* `:54` — *"P4 — all seven (none deprioritized)"*. **Two are not fixed (F7, F8 below, both verified by me).** The P4 table is additionally **renumbered by one** across R11/R13/R14, so its row labelled "R11" describes R9's docstring work and its row labelled "R14" describes the review's R13 (`BaseException`) — the shipped tests are even named `test_r14_*` for what the review called R13. **In fairness:** footnote `:250` *does* honestly disclose both gaps. So this is a headline/footnote contradiction and a numbering error, not concealment. It is still P3: the headline claim is load-bearing for the gate decision, it is false on two counts (and now on a third — the suite is red), and headlines are what get quoted. | `grep -n "R1[0-4]" M4-CANDIDATE-round2-fixes-2026-08-22.md` → row IDs vs `CLAUDE-OPUS-MAX-REVIEW-m4-catalog-builder-fixes-2026-08-22.md`'s R10–R16 table. Underlying code state proven in F7/F8. |
| **F6** | `/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json` | **The live production catalog was written by the not-yet-approved candidate.** It carries `generator.sha256 = 826d5588…` — the exact bytes under review, sitting under a round-1 NO-GO. Round 1 explicitly disclosed it never wrote there. This warrants an explicit decision, not a side effect. **Downgraded from Lens 2's P2** because: SKILL.md itself calls `catalog.json` *"a derived, regenerable convenience file … **not** one of `reviewed-startup-pack-manifest.json`'s pinned `shared_source_sha256s` entries"*; it lives outside any git repo; one rebuild with approved code fully recovers it; and **I verified its current content is sound** — 11/11 structural invariants pass (`counts.*` match array lengths, all histograms sum to `scan_targets`, page∩capability `global_id` = ∅, all `global_id`s distinct, `in_degree == len(referenced_by)` everywhere, dep-state sum == `depends_on_refs`, `degraded_sources: 0`). | `python3 -c "import json; d=json.load(open('…/catalog.json')); print(d['generator']['sha256'], d['generated_at'])"` → `826d5588…  2026-08-22T09:38:12Z`, mtime `17:38:12`. **Not attributable to any reviewer** — both lenses rebound `DEFAULT_OUTPUT_DIR` to scratch and hashed before/after; the write's `generated_at` matches neither's run. It is attributable to a **concurrent session running this untracked candidate against the real manifest** — a coordination hazard worth raising before the gate. |

### P4 (6)

| ID | File:line | Finding | Repro (personally run) |
|---|---|---|---|
| **F7** | `build_cross_project_catalog.py:1494` (`projects_with_sources`) + `:1823` (human summary) | **Round 1's real R11, carried forward unfixed** (the report's "R11" row describes R9's docstring instead). A project whose `wiki/` exists but holds nothing the tool reads still counts as having "contributed sources". Also **entirely untested** — `projects_with_sources` appears **0 times** in the test file, and Lens 2's mutant setting it to the constant `0` survived the whole 86-test suite. | 4 projects, 3 of whose `wiki/` holds only `README.md`:<br>`scan_targets: 4  projects_with_sources: 4  project_status_histogram: {'ok': 4}`<br>`wiki_projects: 0  capability_projects: 1  capabilities: 1  wiki_pages: 0  inventory_projects: 0`<br>human line: `catalog: 4 of 4 enumerated project paths contributed sources`<br>**Latent on the live fleet — I checked: of the 7 `ok` rows in the current production catalog, 7 contributed a usable source, 0 did not.** P4 is the correct (round-1) rating; not inflated for having gone unfixed. |
| **F8** | `build_cross_project_catalog.py:1382`, vs `:1331` (self-reference arm) and `:1357` (resolved arm) | **Round 1's real R14, carried forward unfixed** (the report's "R14" row describes the review's R13). `redundant = scope == "cross-project" and target_project_id == cap["project_id"]` is computed once at `:1298` and applied on two of three arms; the `unresolved` append at `:1382` is a bare 4-key dict. Also **untested** — `redundant_spelling` appears twice in the test file (`:1296`, `:1477`), both on set-cases; Lens 2's mutant *adding* it to the unresolved arm survived the suite. | project `selfp`, cap `a` (`script`/`real.py`) with `depends_on: ["selfp:script:nope.py", "selfp:script:real.py"]`:<br>`state=unresolved      keys=['raw','ref_key','scope','state']                                    redundant_spelling=None`<br>`state=self-reference  keys=['raw','redundant_spelling','ref_key','scope','state','target_global_id']  redundant_spelling=True` |
| **F9** | `build_cross_project_catalog.py:1325-1343` (self-reference arm) vs `:1359-1365` (`credited_targets` dedup) | **Self-reference refs are not deduped across spellings — asymmetric with the R3 fix this round shipped.** `credited_targets` is consulted only on the `resolved` arm, so one logical self-loop written two legal ways is counted twice. No published graph metric is wrong (`in_degree`/`referenced_by` deliberately exclude self-refs, and the code's stated invariant is scoped to those two fields), so nothing is falsified — it is simply inconsistent with R3's own dedup philosophy. Same shape applies to two spellings of one unresolved target. | project `sr`, cap `a` (`script`/`t.py`) with `depends_on: ["script:t.py", "sr:script:t.py"]`:<br>`dep states: ['self-reference','self-reference']`<br>`dup flags : [None, None]`<br>`self_reference_refs: 2 | rows in self_references: 2 | duplicate_edges: 0 | duplicate_depends_on_entries: 0` |
| **F10** | `test_build_cross_project_catalog.py:1053, 1121, 1172, 1223, 1259, 1292, 1323, 1363, 1427, 1467` | **10 × `assertIn(code, (0, 1))` in `EndToEndTests`, 6 of them in tests written this round** (`b1_b`, `r7`, `r6`, `r5`, `r3`×2). The exit code is a **pure deterministic function** of one field — `build_cross_project_catalog.py:1959`: `return 1 if catalog["degraded"] else 0` — so `assertEqual` is always available and these ten sites are structurally blind to a regression that flips a fixture clean↔degraded. Notable because R16's own report row claims it *tightened* the analogous `assertIn(status, ("partial","ok"))` to the deterministic value. | Determinism confirmed at `:1959` by reading, and measured across three of my own fixture builds: `degraded_sources=0 → exit 0`, `degraded_sources=0 → exit 0`, `degraded_sources=1 → exit 1`. Lens 2 separately instrumented three of the ten call sites and measured a deterministic `0` in every case. |
| **F11** | `build_cross_project_catalog.py:479`; `validate_reusable_capabilities.py:88`, `:346`, `:413` | **`ID_RE.match()` admits a trailing newline** — the pattern is `^[a-z0-9]+(-[a-z0-9]+)*$`, Python's `$` matches before a final `\n`, and `.match` (not `.fullmatch`) is used. So `id: "home\n"` is admitted and mints `global_id "nl#home\n"`. **Not a disjointness hole** — `ID_RE.match("page:home\n")` is `False`, so `:` remains barred and F2/R1(a) are unaffected. **Not drift either** — the M3 validator uses `.match` identically at `:346`/`:413`, so this is a shared quirk of the imported grammar, which is exactly what the module's stated anti-drift policy asks for. Worth one line only because `name` **is** newline-rejected via `_name_is_addressable`, making the two halves of a ref_key asymmetric. Both lenses reached this conclusion independently. | `ID_RE.match('home')→True · ID_RE.match('home\n')→True · ID_RE.fullmatch('home\n')→False · ID_RE.match('page:home\n')→False`.<br>End-to-end: project `nl` with `{"id":"home\n",…}` → `caps: [('home\n', 'nl#home\n')]`. **The `--json` one-object-per-line contract survives** (`json.dumps` escapes the `\n`; verified the emitted line is 1 line), so the M3 round-4 output-boundary fix is not defeated. |
| **F12** | `build_cross_project_catalog.py:1691` (`atomic_write_within`) | **`O_NOFOLLOW` at this call site is untested and unreachable-in-effect.** Lens 2's mutant dropping `O_NOFOLLOW` survived the suite; dropping `O_EXCL` was killed. **Benign — I verified the POSIX reasoning first-hand:** with `O_CREAT|O_EXCL`, a symlink at the target path fails `EEXIST` whether or not `O_NOFOLLOW` is set, so `O_NOFOLLOW` can never be the thing that saves you here. Filed only because round 1 credited *"`O_NOFOLLOW` on all four opens"* as a verified property and at this one site nothing pins it. | `ln -s /etc/hosts link` then:<br>`O_CREAT|O_EXCL (no NOFOLLOW) → FileExistsError 17`<br>`O_CREAT|O_EXCL|O_NOFOLLOW  → FileExistsError 17` |

---

## What this round got right — verified first-hand, do not re-raise

I re-derived **every** original repro from round 1's own text and drove it against the current bytes. **All nine of R1–R9 are fixed correctly and completely.** Not one original repro could be made to fire again.

| Round-1 ID | Original failure | Current behaviour I measured | |
|---|---|---|---|
| **R1(a)** | cap `id:"page:home"` + page `id:"home"` → identical `global_id`, exit 0, unflagged | Of 6 caps (`page:home`, `UPPER`, `trailing-`, `has space`, `under_score`, `good`), **only `good` survives**; `OVERLAP: []`; source `partial`; `degraded_sources: 2`; exit 1 | ✅ |
| **R1(b)** | two pages `id:"dup"` → 2 rows, 1 distinct id, exit 0, silent | 3 page rows **retained** (flag, don't drop — as specified); `duplicate_page_global_id: [False, True, True]`; `ambiguous_page_global_ids: ['dupp#page:dup']`; `counts.ambiguous_page_global_ids: 1` | ✅ |
| **R2** | direct call on a FIFO: blocked >8 s, SIGKILL required | Real `mkfifo`, real `_read_previous_catalog`: **returns `None` in 0.0000 s**; the FIFO is still a FIFO (rejected, not drained). Both lenses' counterfactuals (strip `O_NONBLOCK`) reproduce the original hang, proving the flag is load-bearing | ✅ |
| **R3** | `depends_on ×3` → `in_degree: 3`, `referenced_by: 3` from one capability | 3 identical raws → **1** dep entry, `duplicate_depends_on_count: 2`, `counts.duplicate_depends_on_entries: 2`, `in_degree: 1`, `len(referenced_by): 1`. Two-spelling dedup also real (`duplicate_edge`). `in_degree == len(referenced_by)` holds everywhere, including on the live 146-target fleet | ✅ |
| **R4** | `a:b.py`, `" lead.py"` admitted to `capability_ref_index` while `_parse_depends_on_ref` returns `None` | Of 5 names, only `fine.py` survives; `capability_ref_index: ['nm:script:fine.py']`; `partial`; exit 1. `trail.py ` and `nl\nname.py` also correctly dropped. `project_id` deliberately **recorded, not enforced** — and the stated justification checks out empirically | ✅ |
| **R5** | excluded target reported as `capability-not-found` / `in-catalog-with-capabilities` | `capability-ambiguous` / `in-catalog-target-excluded` / `target_excluded_reason: "duplicate-global-id"`. The deliberate `duplicate-ref-key` carve-out is documented at `:1398-1411` and correct | ✅ |
| **R6** | self-ref with barred ref_key → `state:"unresolved"`, `self_reference_refs: 0` | `state: "self-reference"`; `self_references` populated with the full row; `unresolved_references: []`; `self_reference_refs: 1`. Lens 1 additionally verified it survives **all three** exclusion axes, not just the original one | ✅ |
| **R7** | `ambiguous_ref_keys` populated with provably non-ambiguous keys | `ambiguous_ref_keys: []`, `counts: 0`; both keys correctly in `excluded_ref_keys` with `reason: "duplicate-global-id"`; `ambiguous_global_ids: ['pa#x']`. Genuine ref_key duplication still lands in `ambiguous_ref_keys` | ✅ |
| **R8** | garbage inventory → `status: ok`, counted, exit 0 | `schema-invalid`; **0** `cli_inventories` rows; `inventory_projects: 0`; `degraded`; project `partial`; exit 1 | ✅ |
| **R9** | `--i-understand-output-override` wrote a catalog inside a project tree | **Flag deleted outright.** `build --output /tmp/x --i-understand-output-override` → `error: unrecognized arguments`, **exit 2**. **0** occurrences of `i-understand` in production source; the only surviving `SUPPRESS` (`:1781`) is inside an explanatory comment; a regression test pins it at `:1955-1968` | ✅ |

**Corroborating evidence I did not have to re-run:** both lenses independently built their own 13-mutant batteries against these fixes (not reusing the fix agent's) and converged on **12–13 killed**, each by the test written for it — the sole survivor being F12, which I proved benign. Lens 2 separately re-derived 42 structural invariants on a live-fleet build (`DEFAULT_OUTPUT_DIR` rebound to scratch) and reported all 42 passing, with a fingerprint reproducing byte-identically across two forced rebuilds and matching a build from a wholly separate process. I re-checked 11 of those invariants against the current production catalog and all 11 pass.

**No live-fleet regression from the new admission checks.** The current production catalog covers 146 scan targets with `degraded_sources: 0`, 17 capabilities, 43 pages — zero capabilities dropped by the new `ID_RE` / `_name_is_addressable` gates. Both pilot files still validate `{"ok": true, "errors": [], "warnings": []}` (12 caps and 5 caps respectively).

---

## GO / NO-GO

> # NO-GO for the Codex `sol` + `max` final sign-off gate.

Per CLAUDE.md rule 4, a reproducible P1 blocks completion, merge, release, install and deploy; the final `[强制双复核]` gate is triggered only when the work is judged fully complete. **F1 is reproducible, deterministic, and present-tense**, so the work is not complete and the gate must not be dispatched yet.

The verdict does **not** hinge on the P1-vs-P2 label: a red suite blocks the gate at either severity, since the gate reviewer's first action is to run it.

### Must fix before the final gate (blocking)

1. **F1 — make the suite green, and green for structural reasons.** Decouple `RealPilotFileTests` from live mutable content. **Fix all four pin groups, not just the one that fires** — `:604`, `:613`, `:624`/`:626`, and `:640`/`:642-651`/`:666`. `:613` in particular must never `assertEqual` a `content_version`: that counter is designed to increment. Also correct the `:578-579` docstring, which claims a hermeticity the `skipUnless` does not provide. **Acceptance: `python3 -m unittest test_build_cross_project_catalog` → `OK`, and it still passes after a deliberate edit to `完善orca/wiki/orca-context-wiki.json`'s `content_version`.**

2. **F2 — correct the `:588-598` disjointness comment.** Ship it in the same commit. This is the same "false invariant written this round about the M5 join key" class that produced round 1's NO-GO, and it is a one-sentence change (name the `project_id` precondition, or gate the mint on the already-computed `project_id_addressable`).

### Strongly recommended in the same round

3. **F3 — update `orca-context-bridge/SKILL.md`.** Fix the `:826` sentence (the `id`-collision path now records into `excluded_ref_keys`, not `ambiguous_ref_keys`); document `capability-ambiguous` and `in-catalog-target-excluded`; document `excluded_ref_keys[]`, `ambiguous_page_global_ids[]`, and the new per-row fields. The code is right; the contract is stale *because of this round*.

4. **F4 — make an R2 regression fail instead of hang.** Bound `_read_previous_catalog` in `test_r2_fifo_…` with a real timeout (or run it in a subprocess), and fix the `:2144-2146` docstring, which claims a detector that structurally cannot detect. Consider renaming so the flag-spy sorts first.

5. **F5 — correct the fix report.** The `:1`/`:5`/`:54` headline claims must match footnote `:250`; and renumber the P4 table so its IDs correspond to round 1's R10–R16. Either fix **F7** and **F8**, or record them as explicit, labelled deferrals — both are legitimately P4 and deferring them is a defensible call, but claiming them fixed is not.

6. **F6 — decide about production `catalog.json`, and coordinate.** It currently carries the unapproved candidate's `generator.sha256`. Its content is sound and it is regenerable, so the fix is a decision, not a repair: regenerate once with approved code after this round closes. Separately, **a concurrent session is running this untracked candidate against the real manifest path** — worth resolving before the gate so the gate reviewer is not chasing a moving target.

### Cheap, non-blocking

7. **F9** — extend `credited_targets`-style dedup (or at least a `duplicate_*` flag) to the self-reference and unresolved arms, for consistency with R3.
8. **F10** — replace the ten `assertIn(code, (0, 1))` with `assertEqual` on the deterministic value.
9. **F11 / F12** — one-line notes, or leave as-is with the rationale recorded. F11 in particular should stay in lockstep with `validate_reusable_capabilities.py`; changing only one side would create the drift the module's policy exists to prevent.

### After the fix round

Re-review the new candidate hashes (Claude `opus`/`max`), confirming F1's acceptance criterion above, then dispatch the Codex `sol` + `max` final gate with `[强制双复核]` on line 1 of the prompt.

---

## Reviewer's disclosure

- **Nothing was modified, committed, staged, or deployed by this review.** Both candidate files' sha256 are identical at the start and the end of this session; the trust anchor is identical at the start and the end; the rn邮箱 pilot is byte-identical; `catalog.json`'s mtime is unchanged at `17:38:12` throughout my session.
- All fixtures, the staged copy used for the F1 Repro-B proof (`t604.py`), and all harness scripts live only in this session's scratchpad at `…/scratchpad/recon/`. No build in this review wrote to `/Volumes/Extreme SSD/Orca/manifests/`; every catalog I produced came from calling `assemble_catalog()` in-process and was never serialized to disk.
- The one benign side effect is refreshed `__pycache__/*.pyc` mtimes under `orca-context-bridge/scripts/`, from importing the module — confirmed gitignored (`git check-ignore -v` → `.gitignore:1:__pycache__/`), the same effect round 1's R12 documents.
- **Concurrent-session activity is material to this review and is disclosed, not hidden.** During the fix round and the two lens reviews, other sessions changed `完善orca/wiki/orca-context-wiki.json` (17:33:26), `完善orca/wiki/reusable-capabilities.json` (17:38:08), the production `catalog.json` (17:38:12), and `orca-context-bridge/SKILL.md` (17:47:11), and staged eight files into the index. None of it touched the candidate. All of it is why **F1** matters: this worktree is not a quiet place, and a test suite that reads it live cannot produce a trustworthy gate signal.
