# M4 review candidate — `build_cross_project_catalog.py` fixes (2026-08-22)

Review candidate for the M4 read-only cross-project catalog aggregator,
after applying every finding from the 12-agent Workflow
(design → build → 4 independent self-check lenses → completeness critic).

**Nothing here has been committed, staged, or deployed.** See §6.

---

## 1. The exact candidate

Hash-verify against these two files. Both live in the `完善orca` worktree.

| File | sha256 (AFTER fixes) |
|---|---|
| `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/build_cross_project_catalog.py` | `8ef05cff969cbc51911c7b11d1d5819102b1cba55536dc37cf2a1b51ff774c77` |
| `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/test_build_cross_project_catalog.py` | `f4fc7ccb5a4526bd2d9b8ff5c874bb30d3fe1f953faae8f64817d6df8725c6ea` |

These **will not** match the pre-fix baseline that was handed to this
round (`0a88c2d3…` / `c440531a…`). That is expected and correct — the whole
point of this round was to change both files. The baseline hashes were
verified byte-exact *before* the first edit; if you are reviewing a tree
whose hashes match neither the baseline nor the table above, stop, because
something else has edited the files since.

Also touched (documentation only, deliberately **not** deployed):
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/SKILL.md`

Both `.py` files are **untracked** in git (`??`), SKILL.md is modified and
uncommitted.

---

## 2. Fixes applied

### Correctness / schema

| ID | Finding | How it was fixed, and where |
|---|---|---|
| **B1** | `wiki_pages[]` entries had no `global_id` | `summarize_context_wiki()` now emits `global_id = f"{project_id}#page:{pid}"` on every page. The `#page:` prefix makes the page namespace provably disjoint from the capability namespace (capability ids match `^[a-z0-9]+(-[a-z0-9]+)*$`, so they can never contain `:`). Verified on the live fleet: 42/42 pages have it, zero overlap with capability global_ids. |
| **B2** | `source_status_histogram` denominator was ~7, not ~145 | `assemble_catalog()`'s per-target loop gained an `else` branch: when a target has no `sources` key (the `no-wiki-dir` / `path-missing` compaction rule), all three allow-listed filenames are counted as `"absent"`. Every bucket now sums to `scan_targets`. Live: `146` for all three files (was `7`). |
| **P2-1** | Duplicate `(kind, name)` in one project last-write-wins into `capability_ref_index` | Index assembly split into two passes. Pass 1 assigns keys and **counts** them; pass 2 admits a capability as a resolution target only if its keys are unambiguous. A duplicated `ref_key` is dropped from the index entirely (neither entry wins) and appended to the existing `ambiguous_ref_keys[]`. Inbound refs then land as `state: "unresolved"` / `reason: "capability-not-found"`. Never raises. |
| **P2-2** | Duplicate `id` in one project destroyed `capability_reverse_index` rows the same way | Same two-pass mechanism, on `global_id`. Duplicates get no reverse-index row at all (so no consumer can read a merged `in_degree`/`referenced_by`) and are recorded in a new top-level `ambiguous_global_ids[]`, alongside `ambiguous_ref_keys[]`. A duplicated `global_id` **also** bars its `ref_key` from the forward index — resolving to an entry with no trustworthy reverse row is worse than not resolving, and this is the invariant that keeps `capability_reverse_index[target_global]` safe to index directly. |
| **P2-3** | `live_probe_ok` read a flat key that does not exist in the real schema | `summarize_cli_inventory()` now reads `doc["liveProbe"]["ok"]`. Live proof: the real inventory on disk has `liveProbe: {ok: false, passed: 17, failed: ["accounts"]}` and the catalog now reports `live_probe_ok: false` (it reported `null` for every project before). |
| **P3-4** | "file exists but is broken" was misfiled as "project hasn't adopted the file" | `projects_with_capabilities_file` now tracks any **non-`"absent"`** status; a second set `projects_with_usable_capabilities_file` tracks `ok`/`partial`. New classification `reason: "capabilities-file-invalid"` / `target_project_state: "enumerated-file-unreadable"` sits between them. |
| **P3-5** | Self-references resolved normally and inflated `in_degree` | The resolution loop compares the resolved `target_global_id` against the referencing capability's own `global_id`; on a match it emits `state: "self-reference"` (a third state, distinct from resolved/unresolved), skips the `in_degree`/`referenced_by`/`referencing_project_ids` credit, and records the entry in a new top-level `self_references[]`. Matches `validate_reusable_capabilities.py`'s existing `self_reference` error code. |
| **P3-6** | Malformed sub-entries were silently `continue`'d with no trace | Both `summarize_reusable_capabilities()` and `summarize_context_wiki()` count drops into a new per-source `dropped_count` field. `dropped_count > 0` downgrades that source's status to `"partial"` with an explanatory `reason`. `"partial"` is wired through consistently: `process_target()` keeps the surviving rows (it previously discarded everything on any non-`ok` status), `degraded[]` picks it up like any other non-ok status, and the `*_projects` counts now sum `ok + partial`. |
| **P3-7** | `kind` was validated as "non-empty string", never against `KIND_VALUES` | `summarize_reusable_capabilities()` now checks `kind in KIND_VALUES`; a bad kind counts toward `dropped_count` like any other malformed entry. This closes the case of an entry indexed under a `ref_key` that `_parse_depends_on_ref()` could never address. |
| **P3-8** | `os.replace` failure orphaned the tmp file | `atomic_write_within()`'s cleanup bracket now wraps the whole open→write→fsync→replace sequence, so **any** exception unlinks the tmp file before re-raising. |
| **P4-9** | `_CONTROL_CHARS` was dead code | `_parse_depends_on_ref()` now references the named constant instead of an inlined literal tuple. The constant was additionally respelled with `chr(0x2028)`/`chr(0x2029)` so it cannot be mistaken for stray whitespace in a diff. |

### Isolation

| ID | Finding | How it was fixed, and where |
|---|---|---|
| **Iso P2-1** | `--output` was an unpinned write boundary; `os.makedirs` ran before any validation | `cmd_build()` gained a pin: without the override flag, `--output` must be `DEFAULT_OUTPUT_DIR` or a descendant, checked both lexically and post-`resolve()` (so `..` escapes are caught). Rejection is exit `2`, reason `output_dir_not_permitted`. A separate check rejects an output dir that is **itself a symlink** (`output_dir_is_symlink`) — `write_only_within()` deliberately never inspects its own root, so this closes that gap. **Both checks run before `os.makedirs`**, so a rejected run leaves no directory behind. The hidden `--i-understand-output-override` flag (`help=argparse.SUPPRESS`, undocumented in SKILL.md) exists solely so the test suite can use tempdirs; a test asserts it does not appear in `--help`. |
| **Iso P2-2** | A `.pyc` was written into a content-bearing project tree | `sys.dont_write_bytecode = True` set immediately before the sibling import, so the guarantee holds regardless of `PYTHONDONTWRITEBYTECODE`. Empirically confirmed: after deleting the file and running the documented invocation with the env var unset, no `validate_reusable_capabilities.cpython-*.pyc` reappears. |
| **Iso P3-1** | Prose overclaimed "every write-capable call is contained via the guard" | Module docstring rewritten to describe the real mechanism: the pin on `--output`, then **structural** containment (catalog file, tmp sibling, lockfile all derived from one validated `output_dir`) plus `O_NOFOLLOW\|O_EXCL` on every create — explicitly *not* per-call gating. The code was also made match the prose: `write_only_within()`'s return value is now used as `final_path` instead of being discarded, and `output_dir` is re-rooted on `final_path.parent` so the lock and tmp paths derive from the same validated location. |
| **Iso P3-2** | Two self-reads sat outside the stated read boundary with no note | Module docstring now names both explicitly (the sibling import, and `generator_path.read_bytes()` for `generator.sha256`) as a deliberate, benign, read-only carve-out — the interpreter loading and fingerprinting the tool itself. Mirrored in SKILL.md. |
| **Iso P3-3** | `_read_previous_catalog` followed symlinks, seconds after the write guard | Rewritten around `os.open(path, O_RDONLY \| O_CLOEXEC \| O_NOFOLLOW)` with `fstat` regular-file and size checks and a bounded read loop. Every `OSError` (ELOOP included) returns `None`, i.e. "no previous catalog" — matching the existing never-block-a-rebuild policy. A planted symlink can now at worst force a rebuild. |
| **U2** | The U+2028/U+2029 sanitizer covered only the catalog file | Factored into a single reusable `_sanitize_line_separators()` and applied at **all three** `json.dumps` output boundaries: the catalog file write (`_encode_catalog`), the `--json` run summary on stdout, and the fatal-error payload on stderr (`_emit_error`). Mirrors `validate_reusable_capabilities.py`'s `_json_line`. |

### Not reverted, by instruction

**U3** — The build phase added a second self-registration entry
(`reusable-capabilities-validator`) to this project's
`wiki/reusable-capabilities.json`, beyond the originally authorized
`cross-project-catalog-build`. It was **kept deliberately**: the new
same-project `depends_on` needs it to resolve, and the live build confirms
it does (`orca/完善orca#cross-project-catalog-build →
orca/完善orca#reusable-capabilities-validator`, `state: resolved`). That
file was not otherwise touched this round — its sha256 is unchanged across
the whole session (§5).

---

## 3. Test suite

```
$ python3 -m unittest orca-context-bridge/scripts/test_build_cross_project_catalog.py
.....................................................................
----------------------------------------------------------------------
Ran 69 tests in 7.554s

OK
```

**69 passed, 0 failed, 0 errored, 0 skipped.** `python3 -m py_compile` is
clean on both files.

Baseline was 35 tests, all of which still pass. Four of them needed
updating for the `--output` pin (`EndToEndTests._run` and the `t17`
failing-enumeration loop now pass `--i-understand-output-override`, which
is exactly the escape hatch that flag exists for). **34 new tests** were
added:

- `test_b1_*` — every wiki page has a namespaced `global_id`; page and
  capability namespaces are provably disjoint.
- `test_b2_*` — every histogram bucket sums to `scan_targets`, including
  the `no-wiki-dir` / `path-missing` rows that carry no `sources` key.
- `test_p2_1_*` — duplicate `(kind, name)`: neither entry wins the index,
  the key is in `ambiguous_ref_keys`, the inbound ref resolves as
  `capability-not-found`, and neither duplicate absorbs the edge.
- `test_p2_2_*` — duplicate `id`: no reverse-index row for either, recorded
  in `ambiguous_global_ids`, and both `ref_key`s barred from the forward
  index.
- `test_p3_5_*` (×2) — self-reference state, zero `in_degree`, and the
  `resolved + unresolved + self_reference == total` identity; plus the
  redundant cross-project spelling of the same self-reference.
- `test_p3_6_*` (×2) — `dropped_count` + `partial` status for capabilities
  and for wiki pages, survivors still catalogued, `degraded[]` and the
  contributing-project counts wired through, exit code 1.
- `test_p3_7_*` — invalid `kind` dropped, and the corresponding `depends_on`
  string proven unparseable.
- `test_p2_3_*` (×2) — `liveProbe.ok` false and true; absent block still
  yields `null`.
- `test_p3_4_*` (×2) — broken file reports `capabilities-file-invalid`, a
  genuinely un-adopted project still reports `project-has-no-capabilities-file`.
- `SanitizerTests` (×3) — the helper itself, the catalog encoder, and the
  stderr fatal payload boundary.
- `test_u2_json_summary_on_stdout_escapes_u2028` — end-to-end, via a stub
  `orca` binary whose **filename contains a real U+2028**, so the separator
  genuinely reaches `enumeration.orca_bin` in the stdout summary.
- `OutputPinningTests` (×8) — rejection outside the real default dir with
  no directory created; rejection when aimed at a project tree (with a
  byte-identical tree snapshot afterwards); override flag bypasses;
  default dir and descendants accepted; `..` escape caught post-resolve;
  `output_dir_is_symlink` both under the pin and under the override; and
  the override flag absent from `--help`.
- `AtomicWriteCleanupTests` (×3) — tmp cleaned when `os.replace` itself
  fails (repeatedly, so junk cannot accumulate), when the write fails, and
  none left on the success path.
- `PreviousCatalogReadTests` (×3) — planted symlink not followed, regular
  file still read, and missing/unparseable/non-object/directory all
  degrading to `None`.
- `NoBytecodeInProjectTreeTests` (×2) — a staged copy of `scripts/` runs
  the documented invocation with `PYTHONDONTWRITEBYTECODE` **removed from
  the environment** and produces no `__pycache__`; plus a source-order
  check that the assignment precedes the sibling import.

One deliberate honesty note: the stderr fatal-payload sanitizer test drives
`_emit_error` directly rather than through a real fatal path. `str(OSError)`
renders its filename with `repr()`, which already escapes U+2028 into
literal text, so **no current fatal path is known to carry a raw
separator**. The test is boundary defense-in-depth — `message` is
`str(exc)` for arbitrary exceptions via `main()`'s `unexpected_error`
catch-all, and the boundary should not depend on which exception type
reaches it. This is stated in the test's own docstring.

---

## 4. Fresh live-fleet build

```
$ python3 orca-context-bridge/scripts/build_cross_project_catalog.py build --force --json
exit 0
```

| Metric | Value **as of 2026-08-22** |
|---|---|
| `repo_count` | 27 |
| `worktree_count` | 143 |
| `scan_target_count` | 146 |
| `projects_with_sources` (content-bearing) | 7 |
| `project_status_histogram` | `{path-missing: 1, no-wiki-dir: 138, ok: 7}` |
| `wiki_projects` / `wiki_pages` | 7 / 42 |
| `inventory_projects` / `inventory_commands` | 1 / 235 |
| `capability_projects` / `capabilities` | 2 / 16 |
| `depends_on_refs` | 7 (`resolved: 5`, `unresolved: 2`) |
| `degraded_sources` | 0 |

These have drifted from the earlier session's 142/145/7 — expected, and
the reason for the standing instruction in §7.

Validation of the three fixes that are observable on real data:

- **`live_probe_ok` (P2-3)** — real inventory on disk:
  `liveProbe: {ok: false, passed: 17, failed: ["accounts"], ...}`.
  Catalog now reports `live_probe_ok: false`. Before the fix: `null`.
- **`source_status_histogram` denominators (B2)** — all three sum to 146,
  equal to `scan_target_count`:
  - `orca-context-wiki.json` → `{absent: 139, ok: 7}`
  - `orca-cli-capability-inventory.json` → `{absent: 145, ok: 1}`
  - `reusable-capabilities.json` → `{absent: 144, ok: 2}`
- **`wiki_pages[].global_id` (B1)** — 42/42 present, 42/42 well-formed,
  and `{page global_ids} ∩ {capability global_ids} = ∅`.

The live fleet happens to contain no duplicates and no self-references
(`ambiguous_ref_keys: []`, `ambiguous_global_ids: []`, `self_references: []`,
forward index 16 / reverse index 16), so P2-1, P2-2 and P3-5 are covered by
fixture tests rather than by live data. Both live unresolved references
classify correctly:

```
orca/完善orca:script:build_knowledge_graph.py       capability-not-found        in-catalog-with-capabilities
服务器/本机连接服务器方式:script:verify-ssh-routes.sh  project-has-no-capabilities-file  enumerated-not-adopted
```

---

## 5. Untouched-artifact verification

**Trust anchor** — `build_startup_bundle.py` re-hashed after all work:

```
50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe
  /Users/www1adwawd/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py
```

Unchanged, exactly as required. It was never read, imported, or written by
this round.

**`~/.agents/skills/`** — `find ~/.agents/skills -newermt '-2 hours'`
returns nothing. Nothing was deployed there.

**Real pilot files** — identical before and after this round's work
(hashed at the start, re-hashed after the live build):

| File | sha256 (before == after) |
|---|---|
| `完善orca/wiki/reusable-capabilities.json` | `7de6af0f6827b21b81546914c84f992d211d575ed429658bbedc49596d875d18` |
| `完善orca/wiki/orca-context-wiki.json` | `b6b60e74ca2f4026794f0f04b0ecd4bb366b57f7abe2aa4cd2e31ff9018707e2` |
| `rn邮箱/wiki/reusable-capabilities.json` | `d08fb975e71dce0f7724f1c017bd0c5672b2ecbe67d9a3dad7b1ae8473aad398` |
| `rn邮箱/wiki/orca-context-wiki.json` | `762572589f1179d78eb53f2a2a83cfe177f5ad5bb0cd0f70d770a88187c7b1c6` |

**Both pilots still validate clean:**

```json
{"path": ".../完善orca/wiki/reusable-capabilities.json", "ok": true, "project": "orca/完善orca", "capability_count": 11, "errors": [], "warnings": []}
{"path": ".../rn邮箱/wiki/reusable-capabilities.json",  "ok": true, "project": "rn邮箱",        "capability_count": 5,  "errors": [], "warnings": []}
```

---

## 6. Scope statement

- **Nothing was committed.** No `git commit`, no `git add`. Both `.py`
  files remain untracked (`??`); SKILL.md is an uncommitted `M`.
- **Nothing was deployed** to `~/.agents/skills/`. SKILL.md in particular
  stays undeployed, pending its own separate review.
- **`build_startup_bundle.py` sha256 confirmed unchanged** at
  `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe`.
- `wiki/orca-context-wiki.json` was not modified in either pilot project.
- The repo's HEAD moved during this session (`c8279f06f8` →
  `94ba8c5536`) from a **concurrent unrelated session** doing
  prime-agent-integration work. Those commits touch
  `install_prime_agent.py`, its tests, and a report — none of this round's
  files. Do not attribute them here.

---

## 7. Instruction to the reviewer

**The Orca fleet is a live moving target.** The repo/worktree/scan-target
counts in §4 were true at the moment of the build that produced them and
will very likely differ by the time you read this — the earlier session in
this same work stream recorded 142/145/7 where this one records 27
repos / 143 worktrees / 146 scan targets.

Re-derive the counts yourself at review time:

```bash
orca repo list --json
orca worktree list --json
python3 orca-context-bridge/scripts/build_cross_project_catalog.py build --force --json
```

Do **not** treat a diff against the literals in this document as a defect.
What should hold invariantly, regardless of fleet drift, is:

1. every `source_status_histogram` bucket sums to `scan_target_count`;
2. every entry in `wiki_pages[]` has `global_id == f"{project_id}#page:{id}"`;
3. `live_probe_ok` equals the target inventory's real `liveProbe.ok`;
4. `resolved_refs + unresolved_refs + self_reference_refs == depends_on_refs`;
5. every value in `capability_ref_index` has a row in
   `capability_reverse_index`.
