# M6 candidate — `query_catalog.py` search entry point (2026-08-22)

Track: **M6** of the cross-project catalog plan
(`~/.claude/plans/sequential-baking-thunder.md`). Built in
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`. **Not committed, not
deployed** — this is a candidate awaiting its own `[复核]` gate, same
discipline as M4.

---

## Candidate artifacts

| file | sha256 |
|---|---|
| `orca-context-bridge/scripts/query_catalog.py` | `4a1b61a877ae2c13449e22bb538536a4085431db34bc341e59a09a6d740d2932` |
| `orca-context-bridge/scripts/test_query_catalog.py` | `2089497c92a8add310e0d94f16539730f156cc1e36e0486c980394d1021eea79` |

Also modified (workspace only, uncommitted): `orca-context-bridge/SKILL.md`
— one new `### Searching the catalog` subsection appended after
`### Keeping the catalog current`, inside the existing
`## Cross-project capability catalog` section. No other part of SKILL.md was
touched.

Absolute paths:

- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/query_catalog.py`
- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/test_query_catalog.py`
- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/SKILL.md`

---

## What it does

`python3 query_catalog.py search "<keyword>"` reads the already-built
`catalog.json` and matches the keyword case-insensitively as a substring
across **both** entry lists, exactly as the plan's M6 text specifies:

| entry type | identity fields | text field |
|---|---|---|
| `capabilities[]` | `id`, `name` | `summary` |
| `wiki_pages[]` | `id`, `title` | `summary` |

Three ranked tiers, best first — `exact` (keyword *is* the id/name/title),
`identity-substring`, `summary-substring` — with a fully deterministic
tie-break (`project_id`, entry type, `global_id`, catalog order), so the
same catalog and keyword always print the same thing.

Exit codes: `0` matched, `1` no match (grep convention), `2` usage error,
`4` **could not look** (catalog missing / unreadable / unparseable). `1` and
`4` are deliberately distinct: on a machine that has never run
`build_cross_project_catalog.py build` there is no catalog at all, and that
must not read as an all-clear.

Every run leads with `verified_at`'s age and shouts `STALE` past
`--stale-after-hours` (default 6, reusing M7's proposed freshness
threshold rather than inventing a second number). A `verified_at` that is
missing or unparseable counts as stale — unprovable freshness must not read
as proven freshness.

---

## Design decisions worth reviewing

**No write path at all, rather than a guarded one.** The aggregator needs
`write_only_within()`, an output pin, a lockfile, and an atomic
tmp-then-rename because it writes. This script has no output file, no cache,
no lock, no tmp sibling. There is no write guard *because there is nothing
to guard* — adding one would imply a write path exists. Two caveats are
stated in the module docstring rather than promised away: CPython's own
`__pycache__` write when the module is **imported** (structurally
unpreventable from inside a module body; never happens when run as a
script), and whatever the caller's shell does with redirected stdout.

**`O_NOFOLLOW` deliberately NOT used**, diverging from
`build_cross_project_catalog._read_previous_catalog()`. That function needs
it because it re-reads a path its own process validated seconds earlier —
a real TOCTOU window. Here there is no such window and no privilege
boundary: the path is either the documented default or one the caller
typed, the read uses the caller's own credentials, and the result is printed
back to that same caller. Refusing a symlinked catalog would break a
legitimate layout to defend nothing. `O_NONBLOCK` + `S_ISREG` + a 16 MB cap
*are* used — those cover the hazards that actually bite (a planted FIFO
blocking inside `os.open()` itself, a device node, a directory, an oversized
input).

**Sanitizer copied, not imported.** `_sanitize_line_separators()` is the
same two-`str.replace` contract as in `build_cross_project_catalog.py` and
`validate_reusable_capabilities.py`. Importing it would mean importing a
private name from a module whose body performs `sys.path` surgery and pulls
in `validate_reusable_capabilities` — the aggregator's whole dependency
surface, dragged into a tool that reads one JSON file. Same trade
`build_cross_project_catalog.py` itself made for `_reject_duplicate_keys()`.
Test T-92 pins the resulting dependency surface to stdlib exactly.

**Human output gets a stricter boundary than `--json`.** Titles and
summaries are lifted verbatim from *other projects'* files. JSON escaping
keeps a control character intact (correctly — a consumer wants the real
value), whereas a terminal would act on it. `_flatten_for_terminal()`
replaces category-`Cc` characters, U+2028/U+2029, and the bidi
override/isolate block with a space, so a crafted summary cannot forge
output rows (`\n`), erase the line above (`\r`), or emit ANSI (`\x1b`).
ZWJ/ZWNJ are deliberately preserved — they carry real meaning in emoji and
Indic/Persian text.

**NFC + casefold, not `.lower()`.** This machine's project ids and titles
come off an Apple-filesystem path enumeration, which can return decomposed
text; a composed keyword would otherwise silently miss. And casefold is the
Unicode-correct case-fold (`'straße'.lower()` is still `'straße'`). Both are
pinned by tests T-03/T-04, each written so a `.lower()`/no-NFC
implementation fails them.

**Reverse-index fields untouched.** `capability_reverse_index`,
`in_degree`, and `referenced_by` are M5's surface. This script neither reads
nor renders them — no second rendering of semantics the parallel track owns.

---

## Verification (all run for real)

### Compile

```
/usr/bin/python3 -m py_compile query_catalog.py      → OK  (3.9.6, system)
python3           -m py_compile query_catalog.py      → OK  (3.14.6, homebrew)
/usr/bin/python3 -m py_compile test_query_catalog.py  → OK
```

Both interpreters matter: the existing suites document `/usr/bin/python3`
(3.9) as the runner, so `datetime.fromisoformat()` is avoided in favour of
`strptime` (3.9 rejects a trailing `Z`).

### Tests

```
$ /usr/bin/python3 -m unittest test_query_catalog
Ran 53 tests in 0.050s
OK

$ python3 -m unittest test_query_catalog          # 3.14
Ran 53 tests in 0.047s
OK
```

**53 passed, 0 failed, 0 skipped** on both interpreters (T-95/T-96 do not
skip here — the real catalog exists on this machine).

Coverage, by the task's checklist:

| required | tests |
|---|---|
| exact-match ranking above substring | T-01 (exact planted *last* and with the alphabetically last `project_id`, so passing cannot be an artifact of insertion order), T-02, T-06 |
| case-insensitivity | T-03 (`STRASSE` vs `straße` — asserts `.lower()` would fail), T-04 (NFC vs NFD) |
| searches both capabilities and wiki_pages | T-05, T-06, T-07 |
| missing catalog file | T-40, T-41 |
| corrupt / unparseable catalog | T-45 (bad JSON), T-46 (invalid UTF-8), T-47 (top level not an object), T-48 (neither list present), T-49 (duplicate top-level key) |
| staleness-age reporting | T-20…T-27 (fresh, stale, configurable threshold, missing, unparseable, future, human output, humanize boundaries) |
| `--json` boundary sanitization | T-70, T-71 (planted U+2028/U+2029 in another project's summary survives the round trip unescaped-in-value but escaped-in-transport), T-72 (stderr payload) |

Plus, beyond the checklist: T-00 mandatory negative control (proves the
filesystem honours permission bits, so T-90 cannot pass vacuously), T-42
directory, T-43 **FIFO that must not hang** (a blocking `os.open()` would
wedge here — the test terminating is the assertion), T-44 unreadable, T-50
oversized, T-51/T-52/T-53 degraded-but-answering paths, T-60…T-66 CLI and
exit codes, T-73 terminal-injection scrub, T-74 truncation is human-only,
T-90 read-only tree byte-identical, T-91 **`os.open`/`builtins.open`
interceptor** asserting no write flag or write mode is ever requested, T-92
AST-parsed dependency-surface and write-API pin, T-93 `search_catalog()` is
a pure function, T-95/T-96 the fixtures still describe the *real*
`catalog.json`.

### No regression in the neighbouring suite

```
$ /usr/bin/python3 -m unittest test_build_cross_project_catalog
Ran 100 tests in 12.824s
OK
```

### Real invocations against the real catalog

`/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json`
(17 capabilities, 43 wiki pages, 7 projects).

**1. Exact match on a capability `name`:**

```
$ /usr/bin/python3 query_catalog.py search "check_wiki_freshness.py"
catalog last verified 36m ago  (2026-08-22T12:11:40Z)  --  17 capabilities, 43 wiki pages, 7 projects
1 match(es) for "check_wiki_freshness.py"

[1] exact              orca/完善orca  script  wiki-freshness-check
     check_wiki_freshness.py
     path: orca-context-bridge/scripts/check_wiki_freshness.py
     Standalone anytime re-check of whether wiki/orca-context-wiki.json still hashes to its signed manifest pin, instead of discovering the drift at the next SessionStart.
     matched: name   global_id: orca/完善orca#wiki-freshness-check
exit=0
```

**2. Both entry types, all three rank tiers, in one query:**

```
$ /usr/bin/python3 query_catalog.py search "capacity" --limit 3
catalog last verified 36m ago  (2026-08-22T12:11:40Z)  --  17 capabilities, 43 wiki pages, 7 projects
3 match(es) for "capacity"

[1] identity-substring orca/完善orca  script  agent-capacity-preflight
     agent_capacity.py
     path: orca-context-bridge/scripts/agent_capacity.py
     Advisory host-capacity snapshot (CPU/load/memory-pressure plus Orca worktree counts) for sizing a multi-agent wave; never starts, stops, or signals an agent.
     matched: id,name,summary   global_id: orca/完善orca#agent-capacity-preflight

[2] identity-substring orca/完善orca  page  capacity-preflight
     本机 Agent 容量预检
     path: orca-context-bridge/scripts/agent_capacity.py
     无副作用地汇总主机与 Orca 聚合状态；真实红黄绿信号仍完整计算，但仅出现在 advisory_true_recommendation 字段供参考，不再决定本轮新增 worker 上限（该上限自 2026-08-16 起固定为 default 2 / max 3，见 resource-gate）。
     matched: id   global_id: orca/完善orca#page:capacity-preflight

[3] summary-substring  orca/完善orca  page  resource-gate
     主机资源门槛
     path: 本机Claude-Codex-Orca多Agent操作手册.md
     agent_capacity.py 计算 load 与 memory pressure 得出的红黄绿信号；自提交 76e7d88ccb(2026-08-16，用户三次重复要求彻底移除门禁后落地)起该信号仅作为 advisory_true_recommendation 参考输出，不再阻断或封顶新 worker——每次调用固定返回 gate: "gate_removed"、new_workers_de ...
     matched: summary   global_id: orca/完善orca#page:resource-gate
exit=0
```

One capability and two wiki pages, correctly ordered across two tiers, with
the human view's summary truncation visible on `[3]`.

**3. A cross-project hit in a project this workspace has nothing to do
with** (`hgfast/节点专用`) — the plan's actual point:

```
$ /usr/bin/python3 query_catalog.py search "restart-guard" --limit 3
1 match(es) for "restart-guard"

[1] identity-substring hgfast/节点专用  page  soga-restart-guard-remediation-20260811
     Soga restart guard remediation 2026-08-11
     path: wiki/soga-restart-guard-remediation-20260811.md
     A reversible systemd failure-fuse correction was verified without changing Soga configuration or restarting service.
     matched: id   global_id: hgfast/节点专用#page:soga-restart-guard-remediation-20260811
exit=0
```

**4. Deliberate no-match (exit 1, not 0, not 4):**

```
$ /usr/bin/python3 query_catalog.py search "kubernetes-operator"
catalog last verified 36m ago  (2026-08-22T12:11:40Z)  --  17 capabilities, 43 wiki pages, 7 projects
no match for "kubernetes-operator"
  Nothing in the catalog uses that word -- but the catalog only covers projects that have adopted wiki/reusable-capabilities.json or wiki/orca-context-wiki.json.
exit=1
```

**5. Missing catalog is a different answer from no-match:**

```
$ /usr/bin/python3 query_catalog.py search "x" --catalog /nonexistent/catalog.json
error: catalog_missing (/nonexistent/catalog.json)
exit=4

$ /usr/bin/python3 query_catalog.py search "x" --catalog /nonexistent/catalog.json --json
{"ok": false, "reason": "catalog_missing", "message": "/nonexistent/catalog.json"}
exit=4
```

**6. Usage error:**

```
$ /usr/bin/python3 query_catalog.py search "   "
error: empty_keyword (keyword must contain a non-whitespace character)
exit=2
```

---

## Boundary confirmations

- **Trust anchor unchanged.**
  `/Users/www1adwawd/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py`
  = `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe`,
  verified at the start and at the end of this work. Never modified, never
  imported, never depended on (pinned by T-92's AST-parsed dependency-surface
  assertion).
- **Nothing deployed.** `~/.agents/skills/orca-context-bridge/scripts/`
  contains no `query_catalog.py`; nothing was copied there and
  `install_shared.py` was not run.
- **Nothing committed.** `HEAD` is still `c756c85dbc`. Both new files are
  untracked; `SKILL.md` is modified-in-worktree only.
- **The other track's files were not touched.** No reverse-index code, no
  `build_cross_project_catalog.py`, no `catalog.json`.
- **Nothing was written outside the workspace and temp dirs.** The real
  `catalog.json` was only ever read; every test fixture lives under
  `tempfile.mkdtemp()` and is cleaned up (verified: no leftover `qc-*`
  directories).

## Known limits (documented in SKILL.md, not hidden)

- Matching is literal substring, not semantic — `agent_capacity.py` will not
  surface for `throttle`.
- The catalog only covers projects that have adopted
  `wiki/reusable-capabilities.json` or `wiki/orca-context-wiki.json` — today
  7 of ~145 enumerated paths. "Not in the catalog" means "not declared", not
  "does not exist anywhere". The human no-match output says this explicitly.
- Nothing triggers a rebuild; the age line and the `STALE` warning are how a
  caller decides whether a `1` is trustworthy. SessionStart wiring is M7 and
  needs its own review.
