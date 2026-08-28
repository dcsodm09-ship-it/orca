# M4 candidate, round 3 — fixes for the round-2 independent review

**Date:** 2026-08-22
**Review being answered:** `CLAUDE-OPUS-MAX-REVIEW-m4-round2-2026-08-22.md`
(Claude `opus`/`max`, 2 parallel lenses + synthesis — P0=0, **P1=1**, P2=0,
P3=5, P4=6, verdict NO-GO).

## Headline — stated precisely, using round-2's OWN finding IDs

**Correction (round-3 independent review, P3-3):** the first version of
this section renumbered round-2's F7–F12 by shifting them under this
round's own brief labels ("F5" for R11/R14, "F7"/"F8"/"F9" for what were
actually round-2's F10/F11/F12). That produced a headline that asserted
round-2's real F9 was both "done" and, five lines later, "not fixed" — a
direct self-contradiction a gate reviewer would rightly reopen. This
section is rewritten below against round-2's own IDs, verified one at a
time against `CLAUDE-OPUS-MAX-REVIEW-m4-round2-2026-08-22.md`'s F7–F12
table.

> **All six required findings (F1–F6) are addressed.** Of round-2's six
> optional findings: **F7** (`projects_with_sources` overcount) and **F8**
> (`redundant_spelling` missing on the unresolved arm) are fixed with
> tests (both were R11/R14 from round 1, both had zero prior test
> coverage, both mutants now killed). **F10** (10× loose `assertIn(code,
> (0,1))`) is fixed — tightened to the deterministic value. **F11**
> (`ID_RE.match()` admits a trailing newline) is deliberately NOT changed,
> documented in-code as a shared quirk with `validate_reusable_
> capabilities.py` that must not be fixed on one side only. **F12**
> (`O_NOFOLLOW` at `atomic_write_within` untested) is fixed at that call
> site; the round-3 independent review (P4-3) additionally found the
> *lockfile's own* `open()` has the identical gap at a separate call site,
> which this round did not close — recorded, not silently missed.
>
> **The suite is green: 97/97, run against the REAL current pilot files as
> they exist on disk right now.** It was 86 tests, 85 passing, before this
> round.

There is exactly one round-2 finding this round does **not** close, and it
is named in full below rather than in a footnote: round-2's **F9**
(self-reference / unresolved refs not deduped across spellings, P4) was
**not** in this round's brief and is **not** fixed. Nothing else from
round 2 is outstanding.

---

## Integrity gate

| Artifact | sha256 | Verdict |
|---|---|---|
| Trust anchor `~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py` | `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` | **unchanged**, verified at start and at end — never opened for write |
| `orca-context-bridge/scripts/build_cross_project_catalog.py` | before `826d558882c57e7fd316e6cb4f909f1ce54ded850033708a54e2c4adeab99834` → **after `48ed75faf36ac0fb9fbf93f6a5da668e3c0c542dfe426ac7c8697d4342ff671d`** | changed by this round (1994 → 2075 lines) |
| `orca-context-bridge/scripts/test_build_cross_project_catalog.py` | before `75537dbbedc9f6a3c84b0c754dbbb1021a7f4710beffdb3abfb1a9483cd45c92` → **after `14140eda42588454f1bb61496744462d7d1e5f2e8834360a2bd1241b17a6598c`** | changed by this round (2221 → 2641 lines) |
| `orca-context-bridge/SKILL.md` | before `1537d2a4dd7911b5bda6b527764bad8c8e8636d58cf8d25a5f52d9957eee7a99` → **after `94065d6f701b11c286543f9738d827f9929ed0e418dd0375901674eefbb7f390`** | changed by this round (+81 / −13 lines). **Undeployed**, pending its own separate review |
| Pilot `完善orca/wiki/reusable-capabilities.json` | `22a9722cf54be2743dec2f5c1d7426d0fa6989341a9aa80b627077ef1b31cbb2` | **untouched** — byte-identical to the round-2 review's recorded value |
| Pilot `projects/rn邮箱/wiki/reusable-capabilities.json` | `d08fb975e71dce0f7724f1c017bd0c5672b2ecbe67d9a3dad7b1ae8473aad398` | **untouched** — byte-identical to the round-2 review's recorded value |
| `orca-context-bridge/scripts/validate_reusable_capabilities.py` | `da03a22be46d0fd3037155dd6baafc0ba0b7bad97174bca4ce15187f38b2776b` | **untouched** (out of scope by instruction — see F8) |

**Deployment / commit state:**

- **Nothing committed, nothing staged.** `git diff --cached` is empty; both
  candidate files are still untracked (`??`), SKILL.md is `M` in the
  worktree only. HEAD is `c756c85dbc` (moved under this session by a
  concurrent commit; not by this work).
- **Not deployed.** `find ~/.agents/skills -name "*cross_project_catalog*"`
  returns nothing.
- No write to `/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/`
  at any point: the live-fleet build rebound `DEFAULT_OUTPUT_DIR` to
  scratch, and the production `catalog.json`'s sha256 and `st_mtime_ns`
  were captured before and after and are identical.

Both pilots still validate clean after this round:
`{"ok": true, errors: [], warnings: []}` — 12 caps and 5 caps.

---

## F1–F6 (required) — what was fixed, and how

### F1 (BLOCKING) — `RealPilotFileTests` pinned live, mutable, concurrently-edited values

**Fixed.** All four pin groups, not just the one that fired.

`test_build_cross_project_catalog.py:571-813` — the single
`RealPilotFileTests` class was replaced by a shared base plus two
per-project classes:

- `_RealPilotBase` — `setUp` reads all three allow-listed files a **second
  time, directly and independently of the module under test**, and every
  exact assertion below compares the module's *output* against that
  independent read. No scraped literal survives anywhere in the class.
- `RealWanshanOrcaPilotTests`, `RealRnMailPilotTests` — split so one
  violated assertion can no longer mask a second one behind
  `assertEqual`'s fail-fast. That masking is precisely what made round 2's
  Repro B necessary (bumping `11`→`12` revealed `content_version 2 != 1`
  sitting behind it).

Pin-group by pin-group:

| Old pin | New treatment |
|---|---|
| `capability_count == 11` (`:604`) | `assertEqual(count, len(outcome["capabilities"]))`, `assertEqual(count + dropped_count, len(file's capabilities))`, plus `assertGreaterEqual(count, 11)` — the original baseline as a **floor** only |
| `content_version == 1` (`:613`) | `assertEqual(entry["content_version"], file's meta.content_version)` plus `assertGreaterEqual(…, 1)`. This counter **exists to increment** (M2 wiki-freshness); it is now never compared to a frozen number |
| `command_count == 235` (`:624`, `:626`) | derived from the file (`len(commands)`, else `commandCount`), plus `assertGreaterEqual(…, 235)` |
| rn邮箱 id-set (`:640`, `:642-651`), `page_count == 3` (`:666`) | id-set is now `assertEqual(catalog ids, file's ids)` — freshly read, not a literal; `page_count` derived from the file plus `assertGreaterEqual(…, 3)`; page id **order** is asserted against the file too |

Two further brittle spots found while in there and fixed the same way:

- the literal `depends_on_raw` pins for two named rn邮箱 capabilities became
  a general invariant over **every** capability ("the builder's
  `depends_on_raw` equals the file's `depends_on` after the documented
  order-preserving dedup"), plus a **structural** assertion that the pilot
  still declares at least one ref that `_parse_depends_on_ref` classes as
  cross-project. The property the pilot was built to exercise is kept; the
  literal target strings are no longer pinned.
- `declared_path` / `declared_path_matches` now assert the **relation**
  (`matches == (os.path.realpath(declared) == real_path)`) instead of a
  frozen `False`. Fixing 完善orca's known pre-SSD-cutover path drift is a
  legitimate edit and must not turn the suite red.

The `:578-579` docstring's false hermeticity claim is corrected, and the
skip guard was widened from *directory presence* to *present **and**
independently parseable* — a file caught mid-write by a concurrent session
now yields an explicit skip naming the file, not a red suite.

**Acceptance, performed for real (both halves the brief asked for):**

1. *Grown scratch copy.* Harness at
   `…/scratchpad/r3/f1_acceptance.py` copies both pilots' `wiki/` into a
   scratch tree, adds a capability, bumps `meta.content_version` by 41,
   adds a page, and adds a CLI command, then re-points the two test classes
   at the scratch tree:
   `Ran 7 tests … OK — GROWN-CONTENT ACCEPTANCE: PASS run=7 fail=0 err=0 skip=0`
2. *Real files, right now.* See the full-suite output below — **97/97 OK**
   against the live pilots (`capability_count` 12, `content_version` 2,
   `page_count` 15, `command_count` 235 at time of writing; none of those
   numbers appears as an equality target anywhere in the suite).

### F2 (BLOCKING, same commit) — overclaiming disjointness comment

**Fixed, and taken past the minimum.**

*Prose (the required minimum):* `build_cross_project_catalog.py:603-636`
(the comment above the page `global_id` mint at `:646`). The unconditional *"ENFORCED, not assumed … no collision with a page
global_id is constructible"* wording is gone. The comment now says the `id`
axis is enforced via `ID_RE`, states the `project_id` precondition
explicitly, and **spells out the exact counter-example** (project
`foo#page:x` + cap id `bar`, vs project `foo` + page id `x#bar`, both
minting `foo#page:x#bar`). It also points at `project_id_addressable` as
the partial signal the module already tracks.

*Protective check (the brief's "prefer, if cheap"):* rather than restate
the precondition as a promise, the overlap is now **measured** at
`build_cross_project_catalog.py:1228-1245`.
`assemble_catalog()` intersects the two global_id sets for real and
publishes `cross_namespace_global_id_collisions[]` (top level) and
`counts.cross_namespace_global_id_collisions`. Pure reporting — no row
dropped, no source degraded, exit code untouched — for the same reason
`project_id_addressable` is recorded rather than enforced.

The reason `project_id_addressable` is a **sound** early-warning here (and
not just a loosely related signal) is worth recording: a cap `global_id`
can only acquire a `:` from its `project_id`, and any `project_id`
containing `:` gives its `ref_key` 4+ colon-separated parts, which
`_parse_depends_on_ref` refuses — so `project_id_addressable` is `False`
for **every** capability that could take part in such a collision. Verified
on the repro: `capabilities_with_unaddressable_project_id: 1`.

Repro, re-run against the fixed bytes:

```
page gids: ['foo#page:x#bar']
cap  gids: ['foo#page:x#bar']
OVERLAP  : ['foo#page:x#bar']
published cross_namespace_global_id_collisions: ['foo#page:x#bar']
counts.cross_namespace_global_id_collisions: 1
```

Tests: `test_f2_cross_namespace_global_id_collision_is_measured_and_published`
(the adversarial case, end to end through the CLI) and
`test_f2_a_clean_fleet_publishes_an_empty_collision_list` (so a green
result is not vacuous).

### F3 — SKILL.md's documented contract was stale

**Fixed** at `orca-context-bridge/SKILL.md:810-895` (+81 / −13). Not
deployed anywhere; it stays undeployed pending its own separate review.

(a) The closed list of **four** unresolved reasons is now a table of all
**six**, each with its `target_project_state` — including this round's
`capability-ambiguous` / `in-catalog-target-excluded`. Verified against the
code by driving all three parseable arms and reading the emitted rows, not
by reading the source: the live set of reason strings the code can emit is
exactly `{malformed-ref, project-unknown,
project-has-no-capabilities-file, capabilities-file-invalid,
capability-ambiguous, capability-not-found}`.

(b) The false sentence — *"the ambiguous key is dropped from the lookup
indices and recorded in `ambiguous_ref_keys` / `ambiguous_global_ids`"* —
is replaced by an explicit five-bullet breakdown of `ambiguous_ref_keys[]`
vs `excluded_ref_keys[]` (R7's split), including the exact case that made
the old sentence false: two capabilities sharing an `id` with distinct
`(kind, name)` land in `excluded_ref_keys` with
`reason: "duplicate-global-id"` and are correctly **absent** from
`ambiguous_ref_keys`.

(c) Newly documented: `excluded_ref_keys[]`, `ambiguous_page_global_ids[]`,
`cross_namespace_global_id_collisions[]`, `target_excluded_reason`,
`duplicate_page_global_id`, `duplicate_edge`, `duplicate_depends_on_count`,
`project_id_addressable`, and the `counts` integers that mirror each list.

One accuracy correction made while writing it, caught by measuring rather
than assuming: `target_excluded_reason` is **not** on every unresolved row
— the `malformed-ref` arm omits it (along with `ref_key`/`scope`, which are
`null`). The doc says so.

### F4 — an R2 regression hung the suite instead of failing it

**Fixed, both halves.**

- **Ordering:** renamed to `test_r2_a_o_nonblock_is_actually_on_the_open_flags`
  and `test_r2_b_fifo_previous_catalog_returns_instead_of_hanging_forever`,
  so unittest's lexicographic ordering runs the non-blocking flag spy
  **first**. A comment records why the names carry `a`/`b`.
- **Structure:** the FIFO call now runs in a **real subprocess under
  `subprocess.run(timeout=20)`**, so the 20-second budget is an actual
  deadlock detector. On a regression the child wedges, `TimeoutExpired` is
  raised, the child is killed, and the test calls `self.fail(...)` with a
  clear message. The old in-process `assertLess(...)` after the blocking
  call could never fire — it sat behind the very hang it claimed to detect.
- **Docstrings:** both corrected. The flag spy is now labelled as *the*
  genuine fail-fast guard; the FIFO test's docstring no longer claims a
  guarantee the old structure could not deliver.

**Verified by mutation** (drop `os.O_NONBLOCK` from `_read_previous_catalog`
in a scratch copy):

```
[KILL] PreviousCatalogReadTests  in 20.2s -> FAILED (failures=2)
       FAIL: test_r2_a_o_nonblock_is_actually_on_the_open_flags
       FAIL: test_r2_b_fifo_previous_catalog_returns_instead_of_hanging_forever
[KILL] whole suite               in 30.7s -> FAILED (failures=2)
```

Both lenses in round 2 measured **HANG, suite never finishes** for this
same mutant. It now reports, in 30 seconds, with both guards naming
themselves.

### F5 — the two real code gaps behind the round-2 report's contradiction

Both closed, both with tests that would have caught them being unfixed.
(The old report at `M4-CANDIDATE-round2-fixes-2026-08-22.md` is left as it
is, per the brief.)

**R11 — `projects_with_sources` counted "has a `sources` dict", not
"contributed".** Was
`sum(project_status_histogram[ok|partial])`, which counts a project whose
`wiki/` exists but holds none of the three allow-listed files: nothing is
wrong with it, so its status is `ok`, so it was counted as having
"contributed sources" while all three of its sources are `absent`. Now
counted per project by actually inspecting that project's source statuses —
"usable" meaning exactly what it means in `_contributing()`: `ok` or
`partial` on at least one of the three.

Repro on the exact shape the brief specified (3 projects whose `wiki/`
holds only `README.md`):

```
before:  scan_targets: 4  projects_with_sources: 4
         "catalog: 4 of 4 enumerated project paths contributed sources"
after :  scan_targets: 4  projects_with_sources: 1
         "catalog: 1 of 4 enumerated project paths contributed sources"
```

Tests: `test_r11_projects_with_sources_excludes_projects_that_contributed_nothing`
(asserts the count, the three `ok` rows with all-`absent` sources, **and**
the human summary line) and
`test_r11_a_partial_source_still_counts_as_contributing` (the contrast
case, so the fix is not "count only `ok`" — a `partial` source keeps its
survivors and really did contribute).

**R14 — `redundant_spelling` missing on the `unresolved` arm.** Now
computed and attached on **all three** parsed arms (`resolved`,
`self-reference`, `unresolved`); the `malformed` arm cannot carry it,
because `redundant` is only defined after a ref parses, and the doc says so.

```
before: state=unresolved      keys=['raw','ref_key','scope','state']                 redundant_spelling=None
after : state=unresolved      redundant_spelling=True
        state=self-reference  redundant_spelling=True
```

Test: `test_redundant_spelling_is_attached_on_all_three_parsed_arms`, which
also pins the **negative** case (an equally-unresolved but non-redundantly
spelled ref must NOT carry the flag, or the field means nothing) and
asserts the set of states carrying the flag is exactly
`{resolved, self-reference, unresolved}`.

Both were confirmed to have had **zero** coverage before: mutants for each
survived round 2's whole suite. Both mutants are now killed (below).

### F6 — production `catalog.json` coordination status

**No code change** — informational, as instructed.

**Status as of this report: still mismatched, and a human-approved rebuild
is still required after this round closes.**

```
/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json
  generator.sha256 : 826d558882c57e7fd316e6cb4f909f1ce54ded850033708a54e2c4adeab99834   <- round-2 candidate
  generated_at     : 2026-08-22T09:38:12Z
  file sha256      : 3f4ba566c06c731fa0f4ef30f7579310eee9f23585ef7777aac095a6be840530
  this round's candidate sha256: 48ed75faf36ac0fb9fbf93f6a5da668e3c0c542dfe426ac7c8697d4342ff671d
```

So the live production catalog still carries the **round-2** candidate's
generator hash, which no longer matches anything — as expected, since this
round changed the generator again. It was written by a concurrent session
running the still-under-review candidate directly; no fix or review agent
wrote it. **This round did not touch it**: its sha256 and `st_mtime_ns`
were captured immediately before and after the live-fleet build and are
byte-for-byte identical. A final approved rebuild against the approved
bytes is still needed, and remains an explicit human step.

---

## Round-2's F7–F12 — done / skipped, with reasons (IDs match `CLAUDE-OPUS-MAX-REVIEW-m4-round2-2026-08-22.md`'s own table exactly)

| Round-2 ID | Status | Detail |
|---|---|---|
| **F7** — `projects_with_sources` counts a project as "contributed" even when every source is absent/malformed (round-1's R11) | **DONE** | Recomputed to require at least one source with real `ok`/`partial` content. Test added: 3 projects whose `wiki/` holds only `README.md` are excluded; mutant reverting to the old status-based sum is killed. |
| **F8** — `redundant_spelling` missing on the `unresolved` depends_on arm (round-1's R14) | **DONE** | Now computed and attached on all three arms (resolved / self-reference / unresolved), not just two. Mutant dropping it from the unresolved arm is killed; mutant setting it unconditionally is also killed (the flag stays non-vacuous). |
| **F9** — self-reference / unresolved refs not deduped across spellings | **NOT FIXED — explicit deferral** | Not in this round's brief. It is a production behaviour change, not test hygiene, and round 2 itself rated it P4 with no published metric wrong (`in_degree`/`referenced_by` deliberately exclude self-refs). Repro still reproduces exactly as round 2 recorded it. Recorded here so it is not mistaken for "done". |
| **F10** — 10 × `assertIn(exit_code, (0,1))` | **DONE** | All ten tightened to `assertEqual(code, 0)`. The exit code is `return 1 if catalog["degraded"] else 0` — a pure function of one field — and none of these ten fixtures drops an entry, so `0` is the only correct answer. Verified by running: all ten pass at the tightened value. A comment at the first site records why. |
| **F11** — `ID_RE.match()` admits a trailing `\n` | **DELIBERATELY UNCHANGED, documented** | Modifying `validate_reusable_capabilities.py` is out of this round's scope, so the "leave both alone and note the shared quirk in a comment" option is the one taken. A comment at `build_cross_project_catalog.py:480-493` records: the quirk, that the M3 validator uses `.match` identically (so this is shared grammar, not drift), that it is **not** a disjointness hole (`ID_RE.match("page:home\n")` is `False`), that `json.dumps` escapes the newline so the `--json` one-object-per-line contract survives, and that **if it is ever fixed, both files must change in the same commit**. No new inconsistency was created. |
| **F12** — `O_NOFOLLOW` at `atomic_write_within` untested | **PARTIALLY DONE** | `test_f9_tmp_file_open_flags_include_o_nofollow_and_o_excl` spies the open flags on the tmp-file open (asserting `O_NOFOLLOW`, `O_EXCL`, `O_CREAT`, `O_WRONLY`); mutation-verified, dropping `O_NOFOLLOW` there is now killed (it survived round 2's suite). **Not closed at a second site**: the round-3 independent review (P4-3) found the *lockfile's own* `open()` has the identical untested `O_NOFOLLOW` gap, which this fix did not reach. Same POSIX reasoning applies (`O_CREAT|O_EXCL` alone already raises `EEXIST` on a symlink), so it is benign, but the "all opens are pinned" property is still half-true. |

**Additional test hygiene done this round, not tied to a specific round-2 ID:** `RealPilotFileTests` split into two classes so one failing assertion cannot mask a second (the exact masking that required round 2's Repro B); the skip guard widened from directory-presence to present-and-parseable (see the corrected wording in the test file itself — the round-3 review's P3-4 found the original wording overstated what this actually covers, since a directory that exists but is missing a file still fails rather than skips; fixed in-code, not just here); `declared_path_matches` asserted as a relation rather than a frozen boolean; `page_count`/`capability_count` cross-checked against `dropped_count` so the counts are proven consistent, not just large enough.

---

## Re-verification, performed for real

**1. `py_compile` — clean on both files.**

```
python3 -m py_compile orca-context-bridge/scripts/build_cross_project_catalog.py \
                     orca-context-bridge/scripts/test_build_cross_project_catalog.py
OK
```

**2. Full suite against the REAL current pilot files — green, right now.**

```
$ date                       2026-08-22T18:23:36+0800
$ cd orca-context-bridge/scripts && python3 -m unittest test_build_cross_project_catalog
.................................................................................................
----------------------------------------------------------------------
Ran 97 tests in 22.976s

OK
$ date                       2026-08-22T18:23:59+0800
```

No skips: both `RealWanshanOrcaPilotTests` (3 tests) and
`RealRnMailPilotTests` (4 tests) **ran** against the live files, whose
current values are `capability_count` 12, `content_version` 2, `page_count`
15, `command_count` 235 — every one of which was a hard-coded equality
target before this round and is now derived. Verbose run confirms each of
the seven as `ok`, not `skipped`.

Earlier identical green run at `2026-08-22T18:18:32+0800`
(`Ran 97 tests in 10.955s — OK`), i.e. reproducible, not a one-off.

**3. Trust anchor unchanged.**
`50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` —
verified at the start of this round and again at the end. Never opened for
write.

**4. Fresh real build against the live fleet (`build --force --json`),
output redirected to scratch.**

`DEFAULT_OUTPUT_DIR` rebound to a scratchpad directory (the
`OutputPinningTests` idiom) so the production manifest path could not be
written even accidentally. Exit `0`, 2.86 s, 146 scan targets:

```
scan_targets 146 · projects_with_sources 7 · wiki_projects 7 · wiki_pages 43
capability_projects 2 · capabilities 17 · inventory_projects 1 · inventory_commands 235
depends_on_refs 7 (resolved 5, unresolved 2, self 0)
ambiguous_ref_keys 0 · excluded_ref_keys 0 · ambiguous_global_ids 0
ambiguous_page_global_ids 0 · cross_namespace_global_id_collisions 0
capabilities_with_unaddressable_project_id 0 · degraded_sources 0
```

**22 / 22 structural invariants PASS**, including all the ones round 2
checked plus the three this round adds:

- every `counts.*` equals its array's length (9 checks);
- all three source histograms and the project histogram sum to
  `scan_targets` (4 checks);
- page ∩ capability `global_id` = ∅, **and** the published
  `cross_namespace_global_id_collisions` equals the independently measured
  overlap;
- all capability `global_id`s distinct; `in_degree == len(referenced_by)`
  everywhere; `resolved + unresolved + self == depends_on_refs`;
- every `capability_ref_index` value has a reverse row; every excluded
  ref_key is absent from the forward index; `ambiguous_ref_keys ⊆
  excluded_ref_keys`;
- **R11 cross-check:** `projects_with_sources` (7) equals an independently
  recomputed count of projects with at least one `ok`/`partial` source (7).
  The old status-based formula also said 7 here — i.e. R11 is genuinely
  latent on this fleet, exactly as round 2 reported; the fix changes no
  live number, only a wrong one on the fixture shape that triggers it.

Production `catalog.json` sha256 and `st_mtime_ns` identical before and
after.

**5. Nothing committed, nothing staged, `~/.agents/skills/` untouched.**
Confirmed above in the integrity gate.

**6. Mutation battery — every new guard kills its mutant.**

| Mutant | Result |
|---|---|
| drop `os.O_NONBLOCK` from `_read_previous_catalog` | **KILL** — 2 failures, whole suite finishes in 30.7 s (round 2: HANG, never finishes) |
| revert `projects_with_sources` to the status-based sum | **KILL** (10.5 s) |
| drop `redundant_spelling` from the `unresolved` arm | **KILL** (9.9 s) |
| force `cross_namespace_global_id_collisions` to `[]` | **KILL** (9.7 s) |
| drop `O_NOFOLLOW` from the `atomic_write_within` tmp open | **KILL** (0.1 s) — survived round 2's suite |

---

## What a reviewer should re-derive first

1. `python3 -m unittest test_build_cross_project_catalog` from
   `orca-context-bridge/scripts` → expect **97 tests, OK**, with the seven
   `RealWanshanOrcaPilotTests`/`RealRnMailPilotTests` methods **running**
   (not skipped) against whatever the pilot files contain at that moment.
2. Edit `完善orca/wiki/orca-context-wiki.json`'s `meta.content_version` in a
   scratch copy and add a capability, and confirm the suite still passes —
   `…/scratchpad/r3/f1_acceptance.py` does exactly this and reports
   `PASS run=7 fail=0 err=0 skip=0`.
3. Re-run round 2's F2 repro (project dir `foo#page:x` + project `foo` with
   page id `x#bar`) and confirm the collision is now **published**, and
   that the page mint-site comment no longer claims it cannot exist.
4. Drop `os.O_NONBLOCK` in a scratch copy and confirm the suite **reports**
   rather than hangs.

## Scratch artifacts (this session only, nothing in the repo)

`…/6c489df8-…/scratchpad/r3/` — `repro.py` (F2/R11/R14 repros),
`f1_acceptance.py` (grown-content acceptance), `mutate.py` /
`mutate_r2.py` (mutation battery), `build-out/catalog.json` (the live-fleet
build). The only side effect on the repo is refreshed `__pycache__/*.pyc`
mtimes under `orca-context-bridge/scripts/`, which is gitignored.
