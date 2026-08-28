# Reconciled independent review — M4 `build_cross_project_catalog.py` fix candidate

**Reviewer:** Claude `opus` / `max`, read-only reconciliation pass over three independent parallel review lenses
**Date:** 2026-08-22
**Candidate:** `orca-context-bridge/scripts/build_cross_project_catalog.py` + `orca-context-bridge/scripts/test_build_cross_project_catalog.py`
**Candidate report under review:** `M4-CANDIDATE-build-cross-project-catalog-fixes-2026-08-22.md`
**Purpose of this pass:** decide whether the candidate proceeds to a Codex `gpt-5.6-sol` + `max` final sign-off gate.

This document reconciles three independent review lenses (Lens 1: correctness + B1 diagnosis; Lens 2: isolation + judgment calls; Lens 3: fresh bug hunt) into one verdict. **Every finding below was re-reproduced first-hand by this reviewer** against the real module — no lens's claim is passed through on trust, and two lens severities were adjusted after verification. Where lenses disagreed, the disagreement is named and resolved with evidence.

---

## HEADLINE VERDICT

> ### NO-GO — one more fix round required before the Codex sol+max final gate.
>
> **P0 = 0 · P1 = 0 · P2 = 3 · P3 = 6 · P4 = 7 (16 total, after dedup).**
>
> The project's blocking rule ("no reproducible P0/P1") is **technically clear** — there is no P0 and no P1. This is nevertheless a NO-GO on readiness, because **two of the three P2s falsify documentation written in this very round**, and the third publishes a wrong number in the catalog's primary graph metric at exit 0. All three lenses independently reached the same operational conclusion: fix the P2s and re-hash before the final gate. A re-hash produces a new candidate, so the final gate has not yet begun in any case.

The 17 claimed fixes in the candidate report are **genuine**. All three lenses verified them independently and I re-confirmed the load-bearing ones. The suite is real: **69/69 pass** (verified), and Lens 2's 7-mutant kill test showed the new tests genuinely bite. This is not a rejection of the round's work — it is a statement that three of its own new invariants are not yet true as written.

---

## Integrity gate — PASSED (verified at start AND at end of this session)

| Artifact | sha256 | Verdict |
|---|---|---|
| Trust anchor `~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py` | `50721e76…8dceccbe` | **unchanged** ✅ |
| `orca-context-bridge/scripts/build_cross_project_catalog.py` | `8ef05cff…ff774c77` | matches claim ✅ |
| `orca-context-bridge/scripts/test_build_cross_project_catalog.py` | `f4fc7ccb…8725c6ea` | matches claim ✅ |
| Pilot `完善orca/wiki/reusable-capabilities.json` | `7de6af0f…d875d18` | byte-identical, `ok:true errors:[] caps:11` ✅ |
| Pilot `projects/rn邮箱/wiki/reusable-capabilities.json` | `d08fb975…3aad398` | byte-identical, `ok:true errors:[] caps:5` ✅ |

- **Nothing committed by this review.** `HEAD` = `223ace9000f305a42d977e172b63cec00b86e7fd`. `git diff --cached` empty. Both candidate files remain untracked (`??`).
  - *Note for the record:* `HEAD` advanced from `94ba8c5536` (as seen by Lenses 2 and 3) to `223ace9000` **during** this review window. That commit is `round 64` of the unrelated concurrent `prime-agent-integration` work (`install_prime_agent.py` + its tests only). It touches nothing in this candidate's blast radius. This is the known concurrent-session pattern in this worktree, not a conflict.
- **Nothing deployed.** `build_cross_project_catalog.py` does not exist under `~/.agents/skills/orca-context-bridge/scripts/` at all.
- **No stale lock.** `/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/` contains only `catalog.json`.

### Live-fleet blast radius: **every finding below is LATENT, not live**

Read-only audit of the current `catalog.json` (146 scan targets, 16 capabilities, 42 pages):

```
page global_ids: total 42, distinct 42, DUPES: []
NAMESPACE OVERLAP page&cap: []
cap ids containing ':' : []      cap names containing ':' : []
duplicated depends_on strings on the live fleet: []
ambiguous_ref_keys: []           ambiguous_global_ids: []
self_reference_refs: 0           degraded_sources: 0
```

No catalog data is wrong today. Every finding requires input the fleet does not currently contain. This is why nothing here is P1.

---

## HEADLINE FINDING — B1 disjointness: **REAL. Severity P2.**

*Requested as a definitive single ruling. This is it, stated so a fix pass can act without re-deriving anything.*

### The claim being made by the candidate

`build_cross_project_catalog.py:500-504`, a comment added **this round** alongside the new page `global_id` field:

```python
# "#page:" keeps the page namespace disjoint from the
# capability namespace: capability ids match
# ^[a-z0-9]+(-[a-z0-9]+)*$ and so can never contain ':',
# meaning no page global_id can ever collide with a
# capability global_id ("<project_id>#<capability id>").
```

The candidate report repeats this as **"provably disjoint"**. It is repeated a third time as an assertion in `test_build_cross_project_catalog.py:925-927`.

### Ruling: the claim is false as implemented, in two independent ways

**The cited regex is never applied by this module.** `^[a-z0-9]+(-[a-z0-9]+)*$` is `ID_RE` at `validate_reusable_capabilities.py:88`. `build_cross_project_catalog.py:92` imports **only** `derive_expected_project_id` from that module — not `ID_RE`. The actual admission test for a capability `id` is `build_cross_project_catalog.py:419-422`:

```python
cid, kind, name = raw.get("id"), raw.get("kind"), raw.get("name")
if not isinstance(cid, str) or not cid:
    dropped += 1
    continue
```

Non-empty string, nothing more. And the module's **own docstring at `:391-392` explicitly forbids the assumption the comment rests on**:

> *"The aggregator must not assume validator-clean input: M3's validator is an opt-in tool, not a gate."*

Note the internal contradiction: `kind` immediately below at `:428` **is** enum-checked, with a comment (`:423-427`) explaining exactly why an unvalidated field must not be trusted. `id` received no such treatment.

**Counterexample B1-a — a capability id can literally be `page:X`** (verified first-hand, driving the real `main() → cmd_build() → assemble_catalog()` path):

```
fixture: project "collide", capability {"id": "page:home", "kind": "skill", "name": "s1"}
                            page       {"id": "home", "title": "Home"}

page  global_ids: ['collide#page:home']
cap   global_ids: ['collide#page:home']
OVERLAP         : ['collide#page:home']
exit: 0 | source status: ok | degraded: [] | ambiguous_global_ids: []
duplicate_global_id flag on the cap: [False]
--> the suite's own B1 assertion (page_gids & cap_gids == set()) would FAIL: True
```

Completely silent. The new duplicate-detection machinery (`:963-1012`) counts capability `global_id`s only against **each other**, never against page ones, so a page/capability collision defeats it by construction.

**Counterexample B1-b — duplicate page ids collide, and needs no adversarial input at all:**

```
fixture: project "dupp", pages [{"id":"dup","title":"First"}, {"id":"dup","title":"Second"}]

wiki_pages rows: 2   distinct global_ids: 1   titles: ['First', 'Second']
source status: ok | page_count: 2 | dropped_count: 0 | degraded: [] | exit: 0
consumer dict {global_id: page} keeps 1 of 2 -> surviving title: ['Second']
```

Also completely silent. `page_count` and `counts.wiki_pages` count **rows**, not distinct pages, so both over-count. Note this is precisely the P2-1/P2-2 failure class the candidate **fixed on the capability side this round** and never applied to the page side.

**No upstream tool catches either case.** `validate_reusable_capabilities.py` is the only validator in `scripts/`, and it covers only `reusable-capabilities.json`. **Nothing in the toolchain validates `orca-context-wiki.json` page ids.**

### Why P2 and not P1, and not P3

- **Not P1:** verified live-fleet-clean (42/42 distinct page global_ids, zero capability ids containing `:`). No data is wrong today, nothing crashes, no security boundary is crossed, and `wiki_pages` is a list so no in-script structure loses a row.
- **Not P3:** `global_id` exists for exactly one reason — to be M5's join key. The natural consumer idiom `{p["global_id"]: p for p in wiki_pages}` silently drops a page (measured: "First" lost). M5 written against the `:500-504` guarantee inherits a false premise, and the test at `:916-928` provides false assurance because its fixture cannot express the failing case. This is a false documented invariant on a field whose entire purpose is downstream joining — the same class the candidate itself rated P2 twice (P2-1, P2-2) and the same overclaiming class as Iso P3-1, which this round already corrected once.

### Concrete fix (three parts — all required)

**(a) Disjointness.** At `build_cross_project_catalog.py:419-422`, enforce the grammar the comment already cites. Preferred form — import it rather than re-derive it:

```python
from validate_reusable_capabilities import ID_RE, derive_expected_project_id   # :92
...
if not isinstance(cid, str) or not ID_RE.match(cid):     # :420, replacing `or not cid`
    dropped += 1
    continue
```

This is consistent with the module's own stated policy (`:44-49`: "two independently maintained copies would drift silently, so it is imported, not reproduced"), and it routes violators into the **existing** `dropped` → `dropped_count` → `status:"partial"` → `degraded[]` → exit 1 machinery — the exact house pattern P3-7 established this round for `kind`. Minimal alternative if importing the regex is undesirable: reject any `cid` containing `":"`. Either closes the collision.

**(b) Uniqueness.** Apply to pages the same two-pass count/flag treatment `:963-1012` applies to capabilities: a `page_global_id_counts` pass, a `duplicate_page_global_id: bool` field on each page row, and a top-level `ambiguous_page_global_ids: []` with a matching `counts.ambiguous_page_global_ids`. **Do not drop the duplicate pages** — flag and retain them, exactly as duplicated capabilities are flagged and retained.

**(c) Prose and test corrections that must ship in the same commit:**
- `:500-504` — rewrite. Half of it is only true once (a) lands; it says nothing at all about (b).
- `test_build_cross_project_catalog.py:916-928` — `test_b1_every_wiki_page_has_a_namespaced_global_id` must gain fixtures that actually exercise both cases (a capability with `id:"page:X"`; a project with two pages sharing an id). Today its `assertEqual(page_gids & cap_gids, set())` passes vacuously.
- The candidate report's B1 row — **"provably disjoint" is not currently earned.**
- Either document that `page_count` / `counts.wiki_pages` count rows rather than distinct pages, or count distinct.

---

## Findings table (deduped across all three lenses)

Corroboration is recorded, not double-counted. "Lenses" shows which of the three independently found it; **all were re-verified first-hand by this reviewer.**

### P0 — none · P1 — none

### P2 (3)

| ID | File:line | Finding | Repro (verified) | Lenses |
|---|---|---|---|---|
| **R1** | `build_cross_project_catalog.py:419-422` (missing check) vs `:500-505` (false claim); test `:916-928` | **B1: the page `global_id` namespace has neither disjointness nor uniqueness enforcement.** See headline section above. | (a) cap `id:"page:home"` + page `id:"home"` → identical `global_id`, exit 0, unflagged. (b) two pages `id:"dup"` → 2 rows, 1 distinct id, `dropped_count:0`, exit 0; consumer dict keeps 1 of 2. | **1, 2, 3** |
| **R2** | `build_cross_project_catalog.py:1311` (open) vs `:1317` (too-late `S_ISREG`); wedge at `:1576` inside the `try` whose `finally: release_lock()` is `:1589-1590` | **`_read_previous_catalog()` blocks forever on a FIFO, wedging the process while it holds the lock — and falsifies its own new docstring.** `O_NOFOLLOW` does not apply to FIFOs, and the `S_ISREG` guard sits *after* the blocking `os.open`. The docstring added this round (`:1298-1308`) promises *"the swap can at worst force a rebuild… never an exception out of this function"*; the real worst case is an indefinite hang holding `.catalog.lock`, so every subsequent run is refused `lock_held` until the 300 s staleness window — after which it wedges too. | Direct call on a FIFO: **still blocked after 8.00 s, required SIGKILL**. Adding `os.O_NONBLOCK` to the same open: **returns `None` in 0.05 s**, and the existing `S_ISREG` check then rejects it correctly. | 2 |
| **R3** | `build_cross_project_catalog.py:437-440` (no dedup), `:1084-1095` (credit loop) | **A duplicated `depends_on` string inflates `in_degree` and `referenced_by`, unflagged, exit 0.** The *same data structure* dedups one of its three fields and not the other two. `duplicate_depends_on` is a dedicated M3 validator error (`validate_reusable_capabilities.py:646`), so this is not hypothetical. Same "fake inbound edge that any M5/M6 graph consumer would take at face value" class the candidate's own `:1042-1045` comment invokes to justify P3-5. | `src#s` with `depends_on: ["tgt:script:t.py"] × 3` → `in_degree: 3`, `len(referenced_by): 3`, `resolved_refs: 3`, from **one** referencing capability; `referencing_project_ids: ['src']` correctly deduped. `degraded: 0`, `ambiguous_ref_keys: []`, exit 0. | 3 |

### P3 (6)

| ID | File:line | Finding | Repro (verified) | Lenses |
|---|---|---|---|---|
| **R4** | `build_cross_project_catalog.py:428-433`, `:967` | **`name` and `project_id` are unvalidated, producing indexed-but-unaddressable `ref_key`s** — the exact defect P3-7 fixed for `kind`, whose own rationale (`:423-427`) is *"indexed under a ref_key that no legal depends_on string could ever address — an unreachable entry masquerading as a live one."* | names `a:b.py` and `" lead.py"` → both admitted to `capability_ref_index` as `nm:script:a:b.py` / `nm:script: lead.py`, `status: ok`, `dropped: 0`, while `_parse_depends_on_ref()` returns **`None`** for both corresponding ref strings. | 2, 3 |
| **R5** | `build_cross_project_catalog.py:1098-1111` | **The unresolved classifier has no arm for "target excluded as ambiguous"**, so a reference to a capability that IS in `capabilities[]` is reported as `capability-not-found` / `in-catalog-with-capabilities` — actively misleading, and the exact misfiled-reason class the candidate's own P3-4 fixed. | `pb` → `pa:script:alpha.py`, where `pa` has a duplicated `id` that bars the ref_key: `reason: "capability-not-found"`, `target_project_state: "in-catalog-with-capabilities"`, while `target IS present in capabilities[]: True`. | 2, 3 |
| **R6** | `build_cross_project_catalog.py:1041` | **A self-reference whose `ref_key` was barred is silently reclassified as a dangling reference.** `:1041` detects a self-loop only through `capability_ref_index.get(ref_key)`; the P2-1/P2-2 bar removes such keys from that index, so the self-reference arm is never reached. Direct P3-5 × P2-1 interaction: the catalog now reports a dangling reference to a capability sitting in its own `capabilities[]`, and disagrees with the validator's `self_reference` verdict — precisely the disagreement P3-5 was added to prevent. | project `sd`: cap `a` (`script`/`same.py`, `depends_on:["script:same.py"]`) + cap `b` (same `kind`/`name`) → `state: "unresolved"`, `self_references: []`, `self_reference_refs: 0`, `reason: "capability-not-found"`. | 3 |
| **R7** | `build_cross_project_catalog.py:997-999`; pinned by test `:1032` | **`ambiguous_ref_keys[]` is populated with provably non-ambiguous keys.** `:997` appends the `ref_key` when *any* of the three axes disqualifies — including `gid_dup`, which says nothing about the ref_key. The field means "excluded", not "ambiguous"; `counts.ambiguous_ref_keys` is therefore a mislabelled number, and a test asserts the current behaviour so a checklist reviewer will not catch it. | project `pa` with ids `x`/`x` but distinct `(kind,name)` → `ambiguous_ref_keys: ['pa:script:alpha.py','pa:skill:beta']`, `counts.ambiguous_ref_keys: 2`, while **real ref_key multiplicities are `{alpha.py: 1, beta: 1}`** — zero ref_keys are ambiguous. | 3 |
| **R8** | `build_cross_project_catalog.py:530-566`, esp. `:533-538` | **`summarize_cli_inventory()` can never degrade** — uniquely among the three summarizers it has no `schema-invalid` and no `partial` path, so a garbage file is reported healthy. Asymmetric with the P3-6 treatment the other two files just received. | `orca-cli-capability-inventory.json = {"totally":"unrelated","not":["an","inventory"]}` → `status: ok`, `command_count: 0`, `degraded: []`, still counted in `inventory_projects`, **exit 0**. Operator gets no signal at all. | 3 |
| **R9** | `build_cross_project_catalog.py:1442-1444` + `:1509`; docstring `:30-32` | **`--i-understand-output-override` is an in-binary bypass of the round's headline pin, gated only by `argparse.SUPPRESS`, and it is demonstrably unnecessary.** *(Severity ruling: Lens 2 filed only the docstring half as P4; Lens 3 filed the flag itself as P3. Resolved to **P3** — see "Resolved disagreements" below.)* | (a) `build --output <project>/wiki --i-understand-output-override` on a fixture project **containing `.git/`** → **exit 0, `catalog.json` written inside the project tree**; the same command without the flag → exit 2 `output_dir_not_permitted`, no dir created. (b) Monkeypatching `bcpc.DEFAULT_OUTPUT_DIR` alone lets `main()` write to a tempdir with **no flag at all**, exit 0 — and `OutputPinningTests` (`:1392`, `:1438`, `:1450`, `:1464`) *already uses exactly that mechanism*. Migrating `EndToEndTests._run` to it would delete the CLI hole outright and make `:30-32`'s unconditional pin claim true rather than "no *documented* invocation". | 2, 3 |

### P4 (7)

| ID | File:line | Finding | Repro (verified) | Lenses |
|---|---|---|---|---|
| **R10** | `build_cross_project_catalog.py:1190-1195` | `_contributing()`'s docstring rationale (*"a source that degraded to 'partial' still contributed real rows"*) is false in the degenerate case. The **decision** is right (see Judgment B); the stated **reason** overstates. It means "the source was usable", not "contributed ≥1 row". | a file whose capabilities are all malformed → `status:"partial"`, `capability_count: 0`, still counted: `capability_projects: 2` while only **1** project contributed any capability. | 2 |
| **R11** | `build_cross_project_catalog.py:1188` + `:1478` | `projects_with_sources` counts `status in ("ok","partial")` — i.e. "has a `wiki/` directory" — but `:1478` prints *"…enumerated project paths **contributed sources**"*. | a `wiki/` holding only `README.md` → project `status: "ok"`, counted: `projects_with_sources: 3 of 5`. Benign live (7/7 genuinely contribute). | 2, 3 |
| **R12** | `build_cross_project_catalog.py:83-92`; test `:1605-1612` | `sys.dont_write_bytecode = True` **structurally cannot protect the module's own `.pyc`** — the import system caches a module's bytecode *before* executing its body, so `:90` is by construction too late for `build_cross_project_catalog` itself. `:83-89` overclaims accordingly. The suite imports the module, so running it drops bytecode into `完善orca`, a scanned project. *(Severity ruling: Lens 3 filed P3; downgraded to **P4** — `__pycache__/` is gitignored (`.gitignore:1`), it is not under any `wiki/`, and the blast radius on both git and the catalog is zero. The guard IS load-bearing for its real scope, the sibling import — Lens 2's mutation control proved that and I reproduced it.)* | staged copy, `PYTHONDONTWRITEBYTECODE` unset: **(A)** `python3 build_cross_project_catalog.py build --help` → no `__pycache__`; **(B)** `import build_cross_project_catalog` → `build_cross_project_catalog.cpython-314.pyc` written (and `validate_reusable_capabilities.cpython-314.pyc` correctly **not** written, confirming the guard's real scope). `test_p2_2_documented_invocation_…` uses only the script form, where CPython never caches `__main__`, so it can never catch this. Fix is docstring scope narrowing, not code. | 3 (1, 2 partial) |
| **R13** | `build_cross_project_catalog.py:1624` | `except Exception` sits under the comment *"this tool must never crash without an exit code"*; `KeyboardInterrupt` / `SystemExit` bypass it. Either `except BaseException` + re-raise, or narrow the comment. (Note `atomic_write_within:1359` already gets this right with `except BaseException`.) | code read; the asymmetry with `:1359` is the evidence. | 3 |
| **R14** | `build_cross_project_catalog.py:1097` | `redundant_spelling` is set on the `resolved` (`:1080`) and `self-reference` (`:1058`) dep entries but omitted on `unresolved`. | unresolved dep entry keys = `['raw','ref_key','scope','state']`; `redundant_spelling` absent. | 3 |
| **R15** | `test_build_cross_project_catalog.py:324-337` | `test_t12_toctou_planted_symlink_at_tmp_path_raises_and_victim_untouched` **calls no production code** — it invokes `os.open()` directly with hand-copied flags and a hand-built `tmp-{pid}-000` name `atomic_write_within` would never generate. It tests the OS, not the module. (Its sibling at `:339` *does* drive `bcpc.atomic_write_within`.) | grep of `bcpc.` in the test body: **0 occurrences**. | 3 |
| **R16** | `test_build_cross_project_catalog.py:1053`, `:1210`, `:1255`, `:1258-1261`, `:1609-1610` | Test-hygiene cluster: four assertions strictly implied by the line above them (`assertNotEqual(state,"resolved")` after `assertEqual(state,"self-reference")`; `assertIsNotNone(x)` after `assertIs(x,False)`; `assertNotEqual(reason,"project-has-no-…")` after `assertEqual(reason,"capabilities-file-invalid")`); one vacuous assertion (`:1258-1261` computes "first gamma ref if any else None" then asserts it is None, in a test where nothing references gamma); and one **factually wrong comment** at `:1609-1610` claiming *"the same phrase also appears in the module docstring"* — the marker `sys.dont_write_bytecode = True` occurs **exactly once** in the module and **zero** times in its docstring. All harmless; the ordering check itself is real. *(One Lens-3 sub-claim adjusted: `:922` is not tautological — it pins the `global_id` format string against production drift.)* | grep counts as stated; assertions read in place. | 3 |

---

## Judgment-call rulings

### Judgment call A — barring a duplicated `global_id`'s `ref_key` from the forward index

> *Does it hide a reference that could legitimately have resolved?*

## **CORRECT. Keep it.** ✅

Two lenses built independent counterfactuals and both got the same result: with only the `gid_dup` term removed from `:997`, a project whose capabilities share an `id` (but have distinct `(kind,name)`) makes the resolution loop raise **`KeyError` at `:1084`** — `rev = capability_reverse_index[target_global]` — which escapes to `main()`'s catch-all and kills the **entire fleet run** with exit 4 `unexpected_error`, no catalog written. The bar is load-bearing, not merely defensive.

The decisive semantic argument: `capability_ref_index`'s *value* is a `global_id`, and that value is the corrupted identity. "Resolving" to `projA#x` does not identify a unique capability when two rows in `capabilities[]` carry that `global_id`. The alternatives are strictly worse — `.get()` in the resolution loop leaves the broken mapping in the index for every downstream consumer to trip over; synthesizing a reverse row recreates exactly the merged `in_degree`/`referenced_by` that P2-2 set out to eliminate. Nothing is lost: the excluded capability stays in `capabilities[]` with `duplicate_global_id: true`, both keys are recorded, and it can still be a resolution *source*. I re-confirmed both stated invariants hold: `ambiguous_ref_keys` is disjoint from `capability_ref_index.keys()`, and every index value has a reverse row.

**Synthesis the individual lenses did not make:** judgment A is correct **and** it generates two of the P3s above as side effects — **R6** (self-references whose ref_key is barred lose their `self-reference` state) and **R7** (`ambiguous_ref_keys` becomes a mislabelled "excluded" list). Keeping the decision therefore *requires* closing R6 and R7; they are the cost of the correct choice, not an argument against it.

### Judgment call B — counting `wiki_projects` / `inventory_projects` / `capability_projects` as `ok + partial`

## **CORRECT. Keep it.** ✅

1. **`ok`-only is demonstrably self-contradictory.** `process_target` keeps surviving rows when status is `partial`, so a project with 10 good + 1 malformed capability contributes 10 rows to `capabilities[]`. Under `ok`-only, `capability_projects` would report **0 projects, 10 capabilities**. The `ok+partial` pairing is coherent.
2. **It matches the other two `ok/partial` call sites**, keeping the catalog internally consistent: `projects_with_sources` (`:1188`) and `projects_with_usable_capabilities_file` (`:924`) — the latter drives the unresolved-reference classifier, so diverging inside `_contributing` would make counts and reasons contradict each other.
3. **Nothing in the schema or SKILL.md defines these counts as "fully clean."** Cleanliness is carried separately and correctly by `degraded[]` / `degraded_sources` / exit 1.
4. Verified consistent everywhere: `wiki_projects` / `inventory_projects` / `capability_projects` each equal `ok+partial` exactly, and all three histograms still sum to `scan_targets`.

**Residual (filed as R10, P4):** in the degenerate all-dropped case the count and the docstring's stated *rationale* diverge. That is a comment nit, not a reason to reverse the decision.

---

## Resolved disagreements between lenses

| Topic | Disagreement | Resolution (verified first-hand) |
|---|---|---|
| **Duplicate page `global_id`s** | Lens 1 folded it into its P2; Lens 2 filed it separately as P3-1; Lens 3 did not raise it. | Merged into **R1** as sub-case (b), at **P2**. Rationale: it shares one root cause with the disjointness half — *the page namespace has zero enforcement of any kind* — and merging avoids double-counting while keeping both halves explicitly enumerated so a fix pass cannot close one and think it is done. **Both (a) and (b) are required fixes.** |
| **`--i-understand-output-override`** | Lens 2 verified the flag as a *correct* isolation property and filed only the docstring omission as P4-2. Lens 3 filed the flag itself as P3-H ("gated only by obscurity, demonstrably unnecessary"). | **Lens 3 is right; ruled P3 (R9).** I reproduced both halves: the flag really does let a write land inside a project tree containing `.git/` at exit 0, **and** the suite can already reach a tempdir with no flag at all by assigning `bcpc.DEFAULT_OUTPUT_DIR` — which `OutputPinningTests` already does at four sites. A hidden test hatch is an acceptable pattern; a hidden test hatch whose stated justification ("it exists so the test suite can…") is falsified by the same suite's own code is a genuine, actionable defect. Lens 2's docstring point is folded in as R9's second half. |
| **`sys.dont_write_bytecode`** | Lens 1: "no `__pycache__` ✓ (informational)". Lens 2: "confirmed + control — the guard is load-bearing." Lens 3 (P3-I): "structurally cannot protect the module's own `.pyc`." | **All three are correct about different halves; ruled P4 (R12).** Verified on a staged copy: the script form writes nothing (guard works for its real scope, the sibling import — Lens 2's mutation control is sound); the import form *does* write the module's own `.pyc`, and the suite uses the import form. **Downgraded from Lens 3's P3** because `__pycache__/` is gitignored (`.gitignore:1`), the directory is not under any `wiki/`, and the blast radius on both git and the catalog is exactly zero. The real defect is the docstring's overclaimed scope. |
| **Overall recommendation** | Lens 1: "DO NOT proceed until the P2 is resolved." Lenses 2 & 3: "proceed — but fix the P2s first and re-hash." | **Not a real disagreement.** "Fix first, then re-hash" produces a new candidate, which means the final gate has not started. Both phrasings mean: one more fix round. Recorded as **NO-GO** to remove the ambiguity. |
| **Coverage differences** | Lens 1 reported only one finding; Lenses 2 and 3 reported many. | Not a conflict — the three lenses had different scopes (correctness+B1 / isolation+judgment / fresh hunt). Lens 1's clean sweep of the 17 claimed fixes stands and is corroborated by my own re-run. |

---

## What the candidate got right (verified, not assumed)

All three lenses independently re-ran the claimed fixes; I re-confirmed the load-bearing ones. **The 17 fixes are genuine.**

- **Suite:** `py_compile` clean; **69/69 pass, 0 failures / errors / skips** — re-run first-hand, and Lens 3 confirmed it on both `/usr/bin/python3` 3.9.6 and `/opt/homebrew/bin/python3` 3.14.6. Lens 2 mutation-tested it with 7 mutants (pin removed, `O_NOFOLLOW` dropped, cleanup bracket shortened, sanitizer→identity, `gid_dup` bar removed, `_contributing`→ok-only, `dont_write_bytecode` dropped) — **all 7 killed**. The new tests genuinely bite.
- **B2** — all three `source_status_histogram` buckets sum to `scan_targets` (146) on the live fleet; was ~7.
- **P2-1 / P2-2** — duplicate `(kind,name)` and duplicate `id` both correctly excluded, recorded, and retained in `capabilities[]`.
- **P2-3** — `liveProbe.ok` now read from the real nested camelCase schema; no inventory anywhere on the fleet has a flat `live_probe_ok` key.
- **P3-4** — four unresolved classifications verified distinct and correct.
- **P3-5** — self-reference state, `in_degree` 0, `referenced_by` empty, `redundant_spelling` on the cross-project spelling. *(Its interaction gap is R6.)*
- **P3-6 / P3-7** — `dropped_count` + `partial` + `degraded[]` + exit 1; `kind` enum enforced.
- **P4-9** — `_CONTROL_CHARS` extracted, no lingering inlined literal.
- **Isolation (Iso P2-1, P3-1/2/3, P3-8):** the output pin rejects a scratch dir, a live project git tree, a same-string-prefix sibling, a post-resolve `..` escape, and a relative path — leaving no stray directory in any case; symlinked output dirs rejected in all three positions; `write_only_within` called once and its return value *is* `final_path`, with `output_dir` re-rooted before lock and write; `O_NOFOLLOW` on all four opens; the `except BaseException` tmp-cleanup bracket survives `IsADirectoryError`, `TypeError` mid-write, an injected `KeyboardInterrupt`, and four consecutive failures with zero leftovers.
- **U+2028/U+2029 sanitization:** verified at all three output boundaries end-to-end, including through a genuine fatal path; the `json.dumps` call-site audit found no unsanitized boundary. The report's own self-flagged weakness (`str(OSError)` renders the separator as ` ` via `repr`) is true and, in any case, moot — the boundary sanitizes unconditionally.
- **Live structural audit:** `resolved + unresolved + self_reference == depends_on_refs`; every `capability_ref_index` value has a `capability_reverse_index` row; `project_id_collisions: []`; fingerprint reproduces byte-identically across rebuilds.
- **Both pilot files byte-identical and still `ok:true, errors:[]`** (11 and 5 capabilities).

**Investigated and refuted — do not re-raise:** `_strip_for_fingerprint` stripping an attacker-supplied `mtime` key (the per-source `sha256` in the same entry still flips the fingerprint); the B2 × P3-6 × P3-7 composition (correct on a 6-target mixed fleet); the forward/reverse index invariant (holds by construction and on live data); lockfile symlink escape (`O_EXCL|O_NOFOLLOW` → `EEXIST`; the stale path unlinks the link, not its target); self-references double-counted as duplicates (does not occur — the failure mode is the opposite one, R6).

---

## GO / NO-GO

# NO-GO for the Codex `sol`+`max` final gate. One more fix round first.

**Precise reading of the rule.** CLAUDE.md rule 4 blocks on *reproducible P0/P1*. There are **none** — so the candidate is not *blocked* in the rule's strict sense. This is a NO-GO on **readiness**, for three reasons:

1. **Two of the three P2s falsify prose written in this very round.** R1's "provably disjoint" and R2's "at worst force a rebuild" are both new claims, and both are false as implemented. Under this project's own protocol, a P0/P1 found at the final gate forces the **entire final gate to be re-run** on a new candidate. Sending known-false invariants into an expensive terminal review is exactly the waste that protocol is designed to avoid — and it is the kind of claim a final-gate reviewer should not have to be the one to catch.
2. **R3 publishes a wrong number in the catalog's primary graph metric at exit 0**, with no `degraded[]` row, no `ambiguous_*` entry, and no exit-code signal. `in_degree` is the headline output of the whole depends_on subsystem.
3. **R2 is a one-line fix that is already verified to work** (`os.O_NONBLOCK` at `:1311`; returns in 0.05 s, and the existing `S_ISREG` check then rejects the FIFO correctly). There is no cost argument for deferring it.

### Must fix before the next review (blocking the final gate)

- [ ] **R1** — B1 namespace. Both sub-fixes: **(a)** enforce `ID_RE` (or minimally reject `":"`) on capability `id` at `:420`, routing violators into the existing `dropped_count`/`partial` machinery; **(b)** two-pass duplicate detection for page `global_id`s with `duplicate_page_global_id` + `ambiguous_page_global_ids[]`. Plus the three prose/test corrections listed in the headline section — including making `test_b1_…` actually exercise both cases.
- [ ] **R2** — add `os.O_NONBLOCK` to `build_cross_project_catalog.py:1311`, and correct the `:1298-1308` docstring. Consider the same one-line hardening at `:333` to close the residual `isfile`→`open` TOCTOU on the read side (not reproduced; noted for completeness — the read side is currently protected by the `os.path.isfile` pre-check at `:296`, which the write side lacks entirely).
- [ ] **R3** — dedup `depends_on_list` at `:437-440` with a recorded `duplicate_depends_on` count, mirroring the M3 validator's error of the same name. Decide and **document** whether `in_degree` counts edges or distinct referencing capabilities, and make `referenced_by` and `referencing_project_ids` agree with that choice.

### Strongly recommended in the same round (all are direct consequences of *this round's own* fixes)

- [ ] **R6** — hoist the self-loop check at `:1041` to compare against `cap["global_id"]` **before** consulting the forward index. This is the cleanest genuine interaction bug among the 17 fixes.
- [ ] **R7** — either rename `ambiguous_ref_keys` to what it means (`excluded_ref_keys`), or populate it only on the `ref_dup` axis and record the other two exclusions separately. The test at `:1032` pins the current behaviour and must move with it.
- [ ] **R5** — add a fifth unresolved reason (e.g. `target-ambiguous` / `target-excluded`) so an excluded target is not reported as `capability-not-found`.
- [ ] **R4** — the same validation pass that fixes R1(a) should cover `name` and `project_id` (minimally: reject `":"`). One edit closes both.
- [ ] **R9** — delete `--i-understand-output-override` and migrate `EndToEndTests._run` to the `DEFAULT_OUTPUT_DIR` monkeypatch `OutputPinningTests` already uses; then `:30-32`'s pin claim becomes unconditionally true. If the flag is deliberately kept, `:30-32` must say so.
- [ ] **R8** — give `summarize_cli_inventory()` a `schema-invalid` path, symmetric with the other two summarizers.

### Documentation / hygiene (not blocking, but cheap and in-scope)

R10, R11, R12, R13, R14, R15, R16 — docstring scope corrections and test-hygiene cleanups. Each is a comment or an assertion, none is a behaviour change.

### After the fix round

Re-hash both files, then re-review the **new** candidate before the Codex `sol`+`max` final gate. Per CLAUDE.md rule 4, the final-gate prompt's first line must carry the `[强制双复核]` marker.

---

## Reviewer's disclosure

- **No file in the repository, in any project tree, in `~/.agents/skills/`, or in `/Volumes/Extreme SSD/Orca/manifests/` was modified by this review.** All fixtures and repro output live in the session scratchpad. Every tool invocation passed an explicit `--output` into the scratchpad; the real `catalog.json` was only ever **read**.
- One benign, gitignored side effect, disclosed for completeness: `python3 -m py_compile` on the two candidate files refreshed the mtimes of `orca-context-bridge/scripts/__pycache__/build_cross_project_catalog.cpython-314.pyc` and `test_build_cross_project_catalog.cpython-314.pyc`. Both files already existed before this session (Lens 3 recorded them at 16:16), `__pycache__/` is gitignored at `.gitignore:1`, and no new path was created. This is also the direct evidence behind R12.
- Nothing committed, nothing staged, nothing deployed. Trust anchor and both candidate hashes re-verified identical at the end of the session.
