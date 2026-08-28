# M5 closure note — 影响面提示(反向索引,仅 `declared_dependency` 一级证据)

**Date:** 2026-08-22
**Track:** M5 of the cross-project catalog plan (`~/.claude/plans/sequential-baking-thunder.md`)
**Repo state:** `完善orca` @ `c756c85dbc61097d3926b5179de82563a3e21cdb`

---

## Verdict

**M5 is ALREADY FULLY SATISFIED by M4's deployed implementation. No code was written, no
file was modified.**

Every literal ask in the M5 plan text is already delivered by
`orca-context-bridge/scripts/build_cross_project_catalog.py`'s top-level
`capability_reverse_index`, and I verified it by re-deriving that index from scratch — with
my own `depends_on` grammar and my own `project_id` derivation, importing nothing from the
aggregator — and diffing against the real deployed `catalog.json`. The two are **byte-for-byte
identical, including `referenced_by` list ordering**. 14/14 independent checks pass.

This is a "no genuine gap found" verdict, not a "looked fine to me" verdict: the verification
scripts are embedded in full below (§7) so a reviewer can re-run them rather than trust this
document.

---

## 1. What M5 literally asks for, and where each ask is already met

> **M5 —— 影响面提示(反向索引,仅 `declared_dependency` 一级证据)**
> 在 M4 聚合器基础上,把各项目 `reusable-capabilities.json` 里的 `depends_on` 声明反转成目标能力条目的
> `referenced_by` 列表。`text_mention`(模糊文本匹配)证据级别本期不做,列入 M8。仍是手动运行。

| # | M5's literal ask | Already delivered by | Evidence |
|---|---|---|---|
| 1 | 在 M4 聚合器基础上 | The reverse index is built inside `build_cross_project_catalog.py` itself (`assemble_catalog()`, L1309–1510) — not a separate tool bolted on | §2 |
| 2 | 把各项目 `reusable-capabilities.json` 里的 `depends_on` 声明反转 | Every enumerated project's `wiki/reusable-capabilities.json` is read (it is one of the three allow-listed filenames); every `depends_on` entry is parsed and reversed | §3, §4 |
| 3 | 成目标能力条目的 `referenced_by` 列表 | `capability_reverse_index[<target global_id>].referenced_by` — one row per target capability, keyed by that capability's `global_id` | §3, §5 |
| 4 | 仅 `declared_dependency` 一级证据 | Edges come **only** from `depends_on` strings. There is no other edge source | §6 |
| 5 | `text_mention`(模糊文本匹配)本期不做 | Grepped the whole aggregator for `.lower()`, `.casefold()`, `.find(`, `startswith`, `re.search`, `difflib`, `SequenceMatcher`, `text_mention`, `fuzzy` — **zero hits**. No fuzzy matching exists to remove or gate | §6 |
| 6 | 仍是手动运行 | No `--background` flag, no `spawn_background()`, not registered in any `settings.json` hook (grepped user + project settings — zero hits). Docstring L63–70 states this and names hook-wiring as M7's gate | §6 |

**Note on ask #3's wording.** M5 says "目标能力条目的 `referenced_by` 列表". M4 publishes this as a
top-level map keyed by the target capability's `global_id`, rather than as a `referenced_by`
field duplicated onto each row of `capabilities[]`. This is the **same data, keyed by the same
target capability**, and it is a deliberate reviewed decision from M4, not an oversight —
`build_cross_project_catalog.py:1303` calls the two-map layout "the documented M5 idiom".
Three reasons the map is the right shape, and the reason it is not a gap:

- It is what the plan's own **验证方式** line asks for verbatim: "`referenced_by` **反向索引**正确"
  (line 83) — a *reverse index*, which is exactly what `capability_reverse_index` is.
- It is the shape M5's own headline (影响面提示) needs: "capability X changed — who might be
  affected?" is **one keyed lookup** returning `referencing_project_ids` directly. A field on
  `capabilities[]` would force a linear scan of the array to find X's row first.
- Duplicating the list onto each capability row would publish the same facts twice in one
  file, which is the exact failure mode the plan's 决策依据 section rules out ("schema 收敛成一个
  文件…不重复三次失败尝试里'四五个文件互相对不上'的模式").

---

## 2. Provenance — the exact artifacts this verdict applies to

Everything below was verified against these bytes. All hashes captured **after** all
verification runs; none of these files was modified by this track.

| File | sha256 |
|---|---|
| `orca-context-bridge/scripts/build_cross_project_catalog.py` | `8ffb5822c30e0a1d10264640678aff065691163f74b77071560704bb57bd5545` |
| `orca-context-bridge/scripts/test_build_cross_project_catalog.py` | `ef496bea7c9df31c53dd519860f00dd9c6f426d340b1c8955b9ac4f1a1929526` |
| `wiki/reusable-capabilities.json` (pilot 1) | `22a9722cf54be2743dec2f5c1d7426d0fa6989341a9aa80b627077ef1b31cbb2` |
| `/Volumes/Extreme SSD/Orca/projects/rn邮箱/wiki/reusable-capabilities.json` (pilot 2) | `d08fb975e71dce0f7724f1c017bd0c5672b2ecbe67d9a3dad7b1ae8473aad398` |
| `/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json` (deployed) | `e1e3fec820564129856de46d4efb7c78c18c8e024307bc967464b4099b0fabc8` |

**Trust anchor, unchanged throughout (checked at start and at end):**
`/Users/www1adwawd/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py`
= `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe`. Never modified, never
imported, never depended on.

**The deployed catalog is current, not stale.** Independently confirmed three ways:

- `generator.sha256` recorded in `catalog.json` (`8ffb5822…`) == live hash of
  `build_cross_project_catalog.py`. The catalog was built by the revision under review.
- Both pilots' recorded `sources["reusable-capabilities.json"].sha256` == the live on-disk
  hash of each file (`d08fb975…`, `22a9722c…`). No source has drifted since the build.
- The re-derivation in §3 reads the **live** files and reproduces the deployed index exactly,
  which is an end-to-end freshness proof on its own.

Catalog built `2026-08-22T12:11:40Z`; `schema_version: 1`; fleet enumeration 27 repos +
143 worktrees → 146 scan targets.

---

## 3. Independent re-derivation of the reverse index (the core evidence)

**Method — deliberately non-circular.** I did *not* import, call, or read back anything from
`build_cross_project_catalog.py` or `validate_reusable_capabilities.py`. I wrote my own:

- `depends_on` grammar (`kind:name` | `project:kind:name`, control-char and whitespace
  rejection, `kind` enum check),
- `project_id` derivation (`workspaces/<a>/<b>` → `"a/b"`, `projects/<n>` → `"n"`),
- forward index, self-loop suppression, per-capability duplicate-target suppression,
- reverse index seeded at `in_degree: 0` for **every** capability.

Then diffed the whole structure against the deployed `catalog.json`.

**I also did not trust the catalog's own project enumeration.** I independently searched the
whole `/Volumes/Extreme SSD/Orca` tree (depth 10, pruning `node_modules`/`.git`/`.venv`/
`__pycache__`) for `reusable-capabilities.json`, to rule out a third adopting project whose
edges the catalog might have missed:

```
./projects/rn邮箱/wiki/reusable-capabilities.json                    <- pilot 2
./workspaces/orca/完善orca/wiki/reusable-capabilities.json           <- pilot 1
./tmp/claude-code-runtime/.../scratchpad/fleet/{alpha,beta,delta}/wiki/reusable-capabilities.json
./tmp/claude-code-runtime/.../scratchpad/{fixtures,victim}/wiki/reusable-capabilities.json
./tmp/claude-code-runtime/.../scratchpad/fleet/outside-target/reusable-capabilities.json
```

The five `tmp/.../scratchpad/` hits are M4's isolation-test fixtures, correctly absent from
the catalog: they are not Orca-enumerated repos or worktrees. **Exactly two real projects have
adopted the file**, matching `counts.capability_projects: 2`.

### 3.1 The reverse index I derived, independently

```
orca/完善orca#agent-capacity-preflight            in_degree=0
orca/完善orca#cross-project-catalog-build         in_degree=0
orca/完善orca#local-wiki-catalog                  in_degree=1  refs=['rn邮箱']
    <- rn邮箱#local-wiki-catalog-adoption   scope=cross-project  raw='orca/完善orca:config-pattern:local-wiki-catalog'
orca/完善orca#orca-context-bridge                 in_degree=0
orca/完善orca#orca-readonly-probe                 in_degree=0
orca/完善orca#prime-agent-verified-install-pipeline in_degree=0
orca/完善orca#reusable-capabilities-validator     in_degree=1  refs=['orca/完善orca']
    <- orca/完善orca#cross-project-catalog-build  scope=same-project  raw='script:validate_reusable_capabilities.py'
orca/完善orca#startup-bundle-verifier             in_degree=1  refs=['orca/完善orca']
    <- orca/完善orca#wiki-freshness-check         scope=same-project  raw='script:build_startup_bundle.py'
orca/完善orca#tmux-agent-mailbox                  in_degree=0
orca/完善orca#wiki-edit-guard                     in_degree=0
orca/完善orca#wiki-freshness-check                in_degree=0
orca/完善orca#write-containment-guard             in_degree=0
rn邮箱#context-graph-rebuild-verify               in_degree=1  refs=['rn邮箱']
    <- rn邮箱#local-wiki-catalog-adoption   scope=same-project  raw='script:rebuild-rn-mail-context-graph.sh'
rn邮箱#evidence-authority-ordering                 in_degree=1  refs=['rn邮箱']
    <- rn邮箱#wiki-private-cache-boundary   scope=same-project  raw='config-pattern:evidence-authority-ordering'
rn邮箱#local-wiki-catalog-adoption                 in_degree=0
rn邮箱#readonly-runtime-snapshot                   in_degree=0
rn邮箱#wiki-private-cache-boundary                 in_degree=0
```

17 rows, 5 edges (4 same-project, 1 cross-project), 2 unresolved, 0 self-references.

### 3.2 Diff against the deployed `catalog.json` — all 14 checks pass

```
PASS  derived project_id matches file's own `project` field (orca/完善orca)
PASS  derived project_id matches file's own `project` field (rn邮箱)
PASS  no duplicate ref_key among pilot capabilities
PASS  no duplicate global_id among pilot capabilities
PASS  same set of reverse-index keys                      -- only-mine=[] only-theirs=[]
PASS  every shared entry byte-for-byte identical (incl. referenced_by list ORDER)
PASS  REQ-2: every capability in capabilities[] has a reverse-index row
PASS  REQ-2: in_degree:0 rows are actually PRESENT (not omitted)  -- 12 of 17 rows are in_degree 0
PASS  in_degree == len(referenced_by) for every row
PASS  referencing_project_ids has no duplicates
PASS  referencing_project_ids == set of project_ids in referenced_by
PASS  row.ref_key matches the owning capability's ref_key
PASS  forward depends_on(resolved) edge set == reverse referenced_by edge set  -- fwd-only=[] rev-only=[]
PASS  total edges match counts.depends_on_state_histogram.resolved  -- edges=5 histogram={'unresolved': 2, 'resolved': 5}
PASS  unresolved-reference set matches
PASS  self-reference set matches
TOTAL: 0 failure(s)   (exit 0)
```

The **bidirectional** check is the strongest of these: I built the edge set from the forward
direction (`capabilities[].depends_on[]` where `state == "resolved"`) and from the reverse
direction (`capability_reverse_index[*].referenced_by[]`) and asserted **set equality in both
directions**. Neither direction has an edge the other lacks. That is what "反转…正确" means.

---

## 4. The one real cross-project impact hint (M5's whole point, on real data)

```
rn邮箱#local-wiki-catalog-adoption
        --depends_on--> "orca/完善orca:config-pattern:local-wiki-catalog"
orca/完善orca#local-wiki-catalog
        referencing_project_ids: ["rn邮箱"]
```

If `完善orca`'s `local-wiki-catalog` convention changes, `catalog.json` already answers
"which other project might be affected?" in one lookup: `rn邮箱`. That is the 影响面提示
deliverable, working, on real hand-written pilot data — not a fixture.

---

## 5. Requirement 2 — `in_degree: 0` rows are present, verified on real data

**12 of the 17 rows have `in_degree: 0`, and all 12 are present in
`capability_reverse_index`.** `len(capability_reverse_index) == counts.capabilities == 17`,
and `ambiguous_global_ids` is empty. So a consumer can distinguish:

- `global_id in capability_reverse_index` with `in_degree: 0` → **"in the catalog; nothing
  declares a dependency on it"**
- `global_id not in capability_reverse_index` → **"not in the catalog"**

This is structural in the code, not incidental: `build_cross_project_catalog.py:1384-1390`
seeds a full zero row for **every** capability during index construction, before any edge is
credited.

**One documented, deliberate exception, honestly stated:** a capability whose `global_id`
collides with another capability's gets **no** reverse row (L1381-1383) and is recorded in
`ambiguous_global_ids[]` instead. That is correct rather than a hole — a shared `global_id`
means the row would be a silent *merge* of two different capabilities' inbound edges (this was
M4 review finding **P2-2**, see `M4-CANDIDATE-build-cross-project-catalog-fixes-2026-08-22.md`
L44), and it is machine-visible rather than silent. It also does not weaken the "distinguish
absent from unreferenced" property, because such a capability is barred from the forward index
too, so nothing can resolve to it. **On real data this exception has zero occurrences**
(`counts.ambiguous_global_ids: 0`).

---

## 6. Scope guards — verified, not assumed

- **No `text_mention` / fuzzy matching to gate (ask #5).** Grep for `.lower()`, `.casefold()`,
  `.find(`, `startswith`, `re.search`, `difflib`, `SequenceMatcher`, `fuzzy`, `text_mention`
  across the whole aggregator: **zero hits**. Evidence level is `declared_dependency` only,
  by construction. Nothing was added here — M8 stays untouched.
- **Manual-run-only (ask #6).** No `--background`, no `spawn_background()`. Grepped
  `~/.claude/settings.json`, `~/.claude/settings.local.json`, `.claude/settings.json`,
  `.claude/settings.local.json` for `build_cross_project_catalog` — **zero hits**. Not
  hook-wired. (Hook wiring is M7 and needs its own review round.)
- **The other track's files were not touched.** M6 owns `query_catalog.py` and the SKILL.md
  update ("更新 SKILL.md 让 agent 知道先查这个再动手做重复的事", plan line 53). I read SKILL.md
  but changed nothing in it.

---

## 7. Acceptance criteria — answered

Plan line 83: *"M5/M6:对至少两个已手工填写 `reusable-capabilities.json` 的试点项目,验证
`query_catalog.py search` 能检索到对方项目的条目,且 `referenced_by` 反向索引正确。"*

This line is shared between M5 and M6. Splitting it:

| Half | Owner | Status |
|---|---|---|
| `referenced_by` 反向索引正确, on ≥2 pilot projects | **M5 (this track)** | **Satisfied, purely by reading the existing `catalog.json`. No new code required.** Both pilots covered; §3.2's 14 checks include the exact-match and bidirectional-consistency proofs. |
| `query_catalog.py search` 能检索到对方项目的条目 | M6 (other track) | Out of this track's scope — `query_catalog.py` does not exist yet and is M6's deliverable. |

### 7.1 Reproduction — script A: independent re-derivation

Run from `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`. Read-only; writes nothing.
The copy I ran is at `<scratchpad>/m5_independent_reverse_index.py`, sha256
`360a5e6e3027be20b83735caf840fa038da3a643cc9728e38e8e3d20ead253ae`. The block below was
extracted back out of this note and diffed against that file to confirm it is **byte-identical**
to what was executed, so this note is genuinely self-contained rather than approximately so.

```python
#!/usr/bin/env python3
"""M5 independent verification.

Re-derives the declared_dependency reverse index FROM SCRATCH out of the two
pilot projects' wiki/reusable-capabilities.json files, with a hand-written
depends_on grammar and a hand-written project_id derivation, importing
NOTHING from build_cross_project_catalog.py / validate_reusable_capabilities.py.
Then diffs the result against the deployed catalog.json's
capability_reverse_index.

Read-only. Writes nothing anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CATALOG = Path("/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json")
PILOTS = [
    Path("/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/wiki/reusable-capabilities.json"),
    Path("/Volumes/Extreme SSD/Orca/projects/rn邮箱/wiki/reusable-capabilities.json"),
]
KINDS = {"skill", "script", "config-pattern"}
BAD_CHARS = ("\n", "\r", "\t", " ", " ", "\x00")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + ((" -- " + detail) if detail else ""))
    if not ok:
        failures.append(label + ((" -- " + detail) if detail else ""))


# --- my own project_id derivation (workspaces/<a>/<b> -> "a/b", projects/<n> -> "n")
def my_project_id(project_root: Path) -> str | None:
    parts = project_root.resolve(strict=False).parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "workspaces":
            rest = parts[i + 1:]
            if len(rest) >= 2:
                return "/".join(rest[:2])
            if len(rest) == 1:
                return rest[0]
            return None
        if parts[i] == "projects" and i + 1 < len(parts):
            return parts[i + 1]
    return None


# --- my own depends_on grammar: "kind:name" | "project:kind:name"
def my_parse(raw: str):
    if not isinstance(raw, str) or not raw:
        return None
    if any(c in raw for c in BAD_CHARS):
        return None
    if raw != raw.strip():
        return None
    parts = raw.split(":")
    if len(parts) not in (2, 3):
        return None
    if any(p == "" or p != p.strip() for p in parts):
        return None
    kind, name = parts[-2], parts[-1]
    if kind not in KINDS:
        return None
    if len(parts) == 2:
        return kind, name, "same-project", None
    return kind, name, "cross-project", parts[0]


# --- load pilots and build my own capability table
my_caps: list[dict] = []
for f in PILOTS:
    root = f.parent.parent
    pid = my_project_id(root)
    doc = json.loads(f.read_text(encoding="utf-8"))
    print(f"[src] {f}\n      derived project_id = {pid!r}  declared project = {doc.get('project')!r}"
          f"  capabilities = {len(doc['capabilities'])}")
    check(f"derived project_id matches file's own `project` field ({pid})", pid == doc.get("project"))
    for c in doc["capabilities"]:
        my_caps.append({
            "project_id": pid,
            "id": c["id"],
            "kind": c["kind"],
            "name": c["name"],
            "global_id": f"{pid}#{c['id']}",
            "ref_key": f"{pid}:{c['kind']}:{c['name']}",
            "depends_on_raw": c.get("depends_on") or [],
        })
print()

# --- forward index (unique keys only, same admissibility rule: no dup ref_key / dup global_id)
from collections import Counter
rk_counts = Counter(c["ref_key"] for c in my_caps)
gid_counts = Counter(c["global_id"] for c in my_caps)
dup_rk = {k for k, n in rk_counts.items() if n > 1}
dup_gid = {g for g, n in gid_counts.items() if n > 1}
check("no duplicate ref_key among pilot capabilities", not dup_rk, str(sorted(dup_rk)))
check("no duplicate global_id among pilot capabilities", not dup_gid, str(sorted(dup_gid)))

fwd = {c["ref_key"]: c["global_id"] for c in my_caps if c["ref_key"] not in dup_rk and c["global_id"] not in dup_gid}

# --- MY reverse index: EVERY capability gets a row, seeded at in_degree 0
mine: dict[str, dict] = {
    c["global_id"]: {"ref_key": c["ref_key"], "in_degree": 0, "referencing_project_ids": [], "referenced_by": []}
    for c in my_caps if c["global_id"] not in dup_gid
}

my_unresolved: list[dict] = []
my_self: list[dict] = []
for c in my_caps:
    credited: set[str] = set()
    seen_raw: set[str] = set()
    for raw in c["depends_on_raw"]:
        if not isinstance(raw, str) or raw == "":
            continue                      # malformed entry, no edge
        if raw in seen_raw:
            continue                      # exact-duplicate raw string, no second edge
        seen_raw.add(raw)
        parsed = my_parse(raw)
        if parsed is None:
            my_unresolved.append({"from": c["global_id"], "raw": raw, "why": "malformed"})
            continue
        kind, name, scope, xproj = parsed
        tproj = xproj if scope == "cross-project" else c["project_id"]
        rk = f"{tproj}:{kind}:{name}"
        if rk == c["ref_key"]:
            my_self.append({"from": c["global_id"], "raw": raw})
            continue                      # self-loop: never an inbound edge
        tgt = fwd.get(rk)
        if tgt is None:
            my_unresolved.append({"from": c["global_id"], "raw": raw, "ref_key": rk, "why": "unresolved"})
            continue
        if tgt in credited:
            continue                      # same target via a 2nd spelling: one edge only
        credited.add(tgt)
        row = mine[tgt]
        row["in_degree"] += 1
        if c["project_id"] not in row["referencing_project_ids"]:
            row["referencing_project_ids"].append(c["project_id"])
        row["referenced_by"].append({
            "project_id": c["project_id"],
            "capability_global_id": c["global_id"],
            "scope": scope,
            "raw": raw,
        })

print("=== MY independently-derived reverse index ===")
for gid in sorted(mine):
    r = mine[gid]
    print(f"  {gid}\n      in_degree={r['in_degree']} refs={r['referencing_project_ids']}")
    for rb in r["referenced_by"]:
        print(f"        <- {rb['capability_global_id']}  scope={rb['scope']}  raw={rb['raw']!r}")
print(f"  (my unresolved: {len(my_unresolved)}, my self-refs: {len(my_self)})")
for u in my_unresolved:
    print(f"        unresolved: {u}")
print()

# --- the deployed catalog
cat = json.loads(CATALOG.read_text(encoding="utf-8"))
theirs = cat["capability_reverse_index"]

print("=== COMPARISON vs deployed catalog.json ===")
check("same set of reverse-index keys", set(mine) == set(theirs),
      f"only-mine={sorted(set(mine) - set(theirs))} only-theirs={sorted(set(theirs) - set(mine))}")

exact = True
for gid in sorted(set(mine) & set(theirs)):
    a, b = mine[gid], theirs[gid]
    if a != b:
        exact = False
        print(f"    DIFF {gid}\n      mine  = {json.dumps(a, ensure_ascii=False, sort_keys=True)}"
              f"\n      their = {json.dumps(b, ensure_ascii=False, sort_keys=True)}")
check("every shared entry byte-for-byte identical (incl. referenced_by list ORDER)", exact)

# --- invariants asserted directly on the DEPLOYED data
check("REQ-2: every capability in capabilities[] has a reverse-index row",
      all(c["global_id"] in theirs for c in cat["capabilities"]),
      str([c["global_id"] for c in cat["capabilities"] if c["global_id"] not in theirs]))
zero = [g for g, r in theirs.items() if r["in_degree"] == 0]
check("REQ-2: in_degree:0 rows are actually PRESENT (not omitted)", len(zero) > 0,
      f"{len(zero)} of {len(theirs)} rows are in_degree 0")
check("in_degree == len(referenced_by) for every row",
      all(r["in_degree"] == len(r["referenced_by"]) for r in theirs.values()))
check("referencing_project_ids has no duplicates",
      all(len(r["referencing_project_ids"]) == len(set(r["referencing_project_ids"])) for r in theirs.values()))
check("referencing_project_ids == set of project_ids in referenced_by",
      all(set(r["referencing_project_ids"]) == {x["project_id"] for x in r["referenced_by"]}
          for r in theirs.values()))
check("row.ref_key matches the owning capability's ref_key",
      all(theirs[c["global_id"]]["ref_key"] == c["ref_key"] for c in cat["capabilities"] if c["global_id"] in theirs))

# --- bidirectional: forward depends_on[] <-> reverse referenced_by[]
fwd_edges = set()
for c in cat["capabilities"]:
    for d in c["depends_on"]:
        if d["state"] == "resolved" and not d.get("duplicate_edge"):
            fwd_edges.add((c["global_id"], d["target_global_id"]))
rev_edges = set()
for gid, r in theirs.items():
    for rb in r["referenced_by"]:
        rev_edges.add((rb["capability_global_id"], gid))
check("forward depends_on(resolved) edge set == reverse referenced_by edge set",
      fwd_edges == rev_edges, f"fwd-only={sorted(fwd_edges - rev_edges)} rev-only={sorted(rev_edges - fwd_edges)}")
check("total edges match counts.depends_on_state_histogram.resolved",
      len(rev_edges) == cat["counts"]["depends_on_state_histogram"].get("resolved", 0),
      f"edges={len(rev_edges)} histogram={cat['counts']['depends_on_state_histogram']}")

# --- unresolved refs I derived vs the catalog's
their_unres = {(u["from"]["capability_global_id"], u["raw"]) for u in cat["unresolved_references"]}
my_unres = {(u["from"], u["raw"]) for u in my_unresolved}
check("unresolved-reference set matches", my_unres == their_unres,
      f"mine={sorted(my_unres)} theirs={sorted(their_unres)}")
check("self-reference set matches", len(my_self) == len(cat["self_references"]) == 0)

print()
print(f"in_degree>0 rows: {sorted((g, r['in_degree']) for g, r in theirs.items() if r['in_degree'])}")
print(f"TOTAL: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
```

### 7.2 Reproduction — script B: synthetic probe for what real data cannot exercise

**Honest limitation of §3:** every row in the real pilot data has `in_degree ≤ 1`, so the real
data does **not** exercise the `referencing_project_ids` dedup path at all — the dedup check in
§3.2 passes vacuously. To close that hole I ran the **real, unmodified** aggregator against a
synthetic throwaway fleet where one project references one target from **two different**
capabilities. Output is redirected into the scratchpad by rebinding `DEFAULT_OUTPUT_DIR` in the
probe process — the same mechanism the project's own test suite uses — so nothing is written
into any project tree or into the real `manifests/` directory (the deployed `catalog.json` hash
was re-checked after the run and is unchanged: `e1e3fec8…`).

Copy at `<scratchpad>/m5_dedup_probe.py`, sha256
`ad93666f2deecc939a943f8b222efab70ae490c0118b7f871ab4825c5b2da068`. Fleet: `hub` (capabilities
`shared`, `lonely`), `consumer` (`c-one`, `c-two`, both → `hub:script:shared.py`), `quiet`
(`untouched`, referenced by nobody).

<details>
<summary>Probe source (verbatim — click to expand)</summary>

```python
#!/usr/bin/env python3
"""M5 evidence supplement: exercise the two reverse-index properties the
REAL pilot data cannot exercise, because every real row has in_degree <= 1.

  (a) referencing_project_ids is DEDUPED when two DIFFERENT capabilities in
      ONE project both reference the same target  -> in_degree 2,
      len(referenced_by) 2, referencing_project_ids ["consumer"] (not
      ["consumer","consumer"]).
  (b) a capability nothing references still gets a reverse-index row, at
      in_degree 0 -- "nothing depends on this" is distinguishable from
      "not in the catalog".

Runs the REAL, UNMODIFIED build_cross_project_catalog.py against a synthetic
throwaway fleet in the scratchpad. Output is redirected the same way the
project's own test suite does it (rebinding DEFAULT_OUTPUT_DIR in this
process), so nothing is written into any project tree or into the real
manifests/ directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = Path("/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts")
ROOT = HERE / "m5probe"
shutil.rmtree(ROOT, ignore_errors=True)
PROJECTS = ROOT / "projects"
OUT = ROOT / "out"
PROJECTS.mkdir(parents=True)
OUT.mkdir(parents=True)

sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("bcpc", SCRIPTS / "build_cross_project_catalog.py")
bcpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bcpc)
bcpc.DEFAULT_OUTPUT_DIR = OUT          # the pin redirect the test suite uses


def cap(cid, kind="script", name="x.py", deps=None):
    return {"id": cid, "kind": kind, "name": name, "path": f"scripts/{name}",
            "summary": "s", "last_verified_at": None, "depends_on": deps or []}


def project(name, caps):
    p = PROJECTS / name
    (p / "wiki").mkdir(parents=True)
    (p / "wiki" / "reusable-capabilities.json").write_text(
        json.dumps({"schema_version": 1, "project": name, "capabilities": caps},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return p


project("hub", [cap("shared", name="shared.py"), cap("lonely", name="lonely.py")])
project("consumer", [
    cap("c-one", name="one.py", deps=["hub:script:shared.py"]),
    cap("c-two", name="two.py", deps=["hub:script:shared.py"]),
])
project("quiet", [cap("untouched", kind="config-pattern", name="untouched")])

repos = {"id": "x", "ok": True, "_meta": {}, "result": {"repos": [
    {"id": f"repo-{n}", "path": str(PROJECTS / n)} for n in ("hub", "consumer", "quiet")]}}
wts = {"id": "x", "ok": True, "_meta": {},
       "result": {"worktrees": [], "totalCount": 0, "truncated": False}}
orca_bin = ROOT / "orca"
data = ROOT / "orca.data"
data.mkdir()
(data / "repo.json").write_text(json.dumps(repos, ensure_ascii=False), encoding="utf-8")
(data / "wt.json").write_text(json.dumps(wts, ensure_ascii=False), encoding="utf-8")
orca_bin.write_text(
    '#!/usr/bin/env python3\nimport sys, pathlib\n'
    'a = sys.argv[1:]\nh = pathlib.Path(__file__).resolve().parent / "orca.data"\n'
    'if a[:2] == ["repo","list"]: sys.stdout.write((h/"repo.json").read_text(encoding="utf-8"))\n'
    'elif a[:2] == ["worktree","list"]: sys.stdout.write((h/"wt.json").read_text(encoding="utf-8"))\n'
    'else: sys.stdout.write("{}")\nsys.exit(0)\n', encoding="utf-8")
os.chmod(orca_bin, 0o755)

code = bcpc.main(["build", "--orca-bin", str(orca_bin), "--output", str(OUT), "--json", "--quiet"])
catalog = json.loads((OUT / bcpc.CATALOG_NAME).read_text(encoding="utf-8"))
rev = catalog["capability_reverse_index"]

print(f"exit code: {code}")
print("reverse index:")
for gid in sorted(rev):
    r = rev[gid]
    print(f"  {gid}: in_degree={r['in_degree']} referencing_project_ids={r['referencing_project_ids']} "
          f"len(referenced_by)={len(r['referenced_by'])}")

fails = []


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + ((" -- " + detail) if detail else ""))
    if not ok:
        fails.append(label)


h = rev["hub#shared"]
check("(a) two distinct referencing capabilities -> in_degree 2", h["in_degree"] == 2, str(h["in_degree"]))
check("(a) referenced_by has both rows", len(h["referenced_by"]) == 2,
      str([x["capability_global_id"] for x in h["referenced_by"]]))
check("(a) referencing_project_ids DEDUPED to one entry",
      h["referencing_project_ids"] == ["consumer"], str(h["referencing_project_ids"]))
check("(b) unreferenced capability in a referenced project has an in_degree-0 row",
      rev["hub#lonely"]["in_degree"] == 0 and rev["hub#lonely"]["referenced_by"] == [])
check("(b) capability in a project nothing references has an in_degree-0 row",
      "quiet#untouched" in rev and rev["quiet#untouched"]["in_degree"] == 0)
check("completeness: every capability in capabilities[] has a reverse-index row",
      all(c["global_id"] in rev for c in catalog["capabilities"]),
      str([c["global_id"] for c in catalog["capabilities"] if c["global_id"] not in rev]))
check("len(capability_reverse_index) == counts.capabilities",
      len(rev) == catalog["counts"]["capabilities"] == 5, f"{len(rev)} vs {catalog['counts']['capabilities']}")
check("in_degree == len(referenced_by) everywhere",
      all(r["in_degree"] == len(r["referenced_by"]) for r in rev.values()))
check("nothing written outside the scratchpad output dir",
      sorted(p.name for p in OUT.iterdir()) == ["catalog.json"], str(sorted(p.name for p in OUT.iterdir())))

print(f"TOTAL: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
```

</details>

```
exit code: 0
reverse index:
  consumer#c-one:   in_degree=0 referencing_project_ids=[]           len(referenced_by)=0
  consumer#c-two:   in_degree=0 referencing_project_ids=[]           len(referenced_by)=0
  hub#lonely:       in_degree=0 referencing_project_ids=[]           len(referenced_by)=0
  hub#shared:       in_degree=2 referencing_project_ids=['consumer'] len(referenced_by)=2
  quiet#untouched:  in_degree=0 referencing_project_ids=[]           len(referenced_by)=0

PASS  (a) two distinct referencing capabilities -> in_degree 2 -- 2
PASS  (a) referenced_by has both rows -- ['consumer#c-one', 'consumer#c-two']
PASS  (a) referencing_project_ids DEDUPED to one entry -- ['consumer']
PASS  (b) unreferenced capability in a referenced project has an in_degree-0 row
PASS  (b) capability in a project nothing references has an in_degree-0 row
PASS  completeness: every capability in capabilities[] has a reverse-index row -- []
PASS  len(capability_reverse_index) == counts.capabilities -- 5 vs 5
PASS  in_degree == len(referenced_by) everywhere
PASS  nothing written outside the scratchpad output dir -- ['catalog.json']
TOTAL: 0 failure(s)
```

`hub#shared` is the decisive row: `in_degree: 2` with **two** `referenced_by` entries but
**one** `referencing_project_ids` entry. The count of *edges* and the deduped list of
*affected projects* are two different numbers, and the aggregator gets both right.

### 7.3 Existing regression coverage

`python3 -m unittest orca-context-bridge/scripts/test_build_cross_project_catalog.py` →
**`Ran 100 tests in 12.798s — OK`** (100/100 green). Run against the unmodified suite as a
baseline; this track changed neither the suite nor the aggregator.

11 of those tests assert reverse-index behaviour directly:

| Test | Pins |
|---|---|
| `test_t16_ref_resolution_same_and_cross_project` | same- and cross-project resolution → `in_degree` / `referencing_project_ids` |
| `test_p2_1_duplicate_kind_name_resolves_to_neither_and_is_recorded` | duplicate `ref_key` absorbs no edge |
| `test_p2_2_duplicate_id_keeps_neither_reverse_index_row` | duplicate `global_id` → no reverse row, recorded in `ambiguous_global_ids` |
| `test_r7_excluded_ref_keys_and_ambiguous_ref_keys_are_different_sets` | exclusion vs ambiguity are different facts |
| `test_p3_5_self_reference_does_not_inflate_in_degree` | self-loop credits nothing |
| `test_p3_5_cross_project_spelled_self_reference_also_caught` | same, spelled cross-project |
| `test_r6_self_reference_survives_its_ref_key_being_barred_from_the_index` | self-loop detection is structural |
| `test_r5_genuinely_absent_capability_still_reports_not_found` | dangling ≠ ambiguous |
| `test_r3_duplicate_depends_on_string_credits_one_edge_and_is_recorded` | identical raw string twice → one edge |
| `test_r3_two_spellings_of_the_same_target_credit_one_edge` | two spellings of one target → one edge |
| `test_grok_p3_capability_id_over_id_max_len_is_dropped` | dropped capability gets no reverse row |

---

## 8. Non-blocking observations (recorded, deliberately NOT acted on)

None of these is a gap in what M5's text asks for; none was fixed, because fixing a non-defect
in a file that already passed three review rounds and shipped would force a fresh review round
for no correctness gain. Recorded so a reviewer can overrule if they disagree.

1. **Real data-quality drift the reverse index correctly surfaced (actionable for the user,
   not for code).** `rn邮箱#context-graph-rebuild-verify` declares
   `depends_on: ["orca/完善orca:script:build_knowledge_graph.py"]`. That script genuinely
   exists on disk (`orca-context-bridge/scripts/build_knowledge_graph.py`, 47904 bytes), but
   `完善orca` has **not** declared it in its own `wiki/reusable-capabilities.json`, so the edge
   cannot resolve. The catalog reports it exactly right — `reason: "capability-not-found"`,
   `target_project_state: "in-catalog-with-capabilities"` — which is the correct diagnosis:
   *the target project has capabilities, but not that one.* The fix is a one-entry addition to
   `完善orca`'s wiki file, i.e. **data, not code**, and it is outside this track's mandate. The
   second unresolved ref (`服务器/本机连接服务器方式:script:verify-ssh-routes.sh`) is the benign
   `project-has-no-capabilities-file` case that resolves itself as adoption spreads.
2. **The "every capability gets a reverse row" invariant is not pinned by a dedicated test.**
   It holds — structurally in the code (L1384-1390), on real data (§5), and on the synthetic
   fleet (§7.2) — and several tests assert `in_degree == 0` for specific capabilities, which
   incidentally requires the row to exist. But no test asserts the invariant *globally* (e.g.
   `{c["global_id"] for c in capabilities} - {ambiguous} == set(capability_reverse_index)`), so
   a future "optimization" that emitted only rows with edges would break M5's contract without
   turning the suite red. Adding that pin is a ~5-line additive test; I did not add it because
   a missing regression pin is not a missing piece of M5's stated deliverable, and test coverage
   was M4's plan step, not M5's. **Recommend it as a small follow-up.**
3. **`SKILL.md` does not document the consumption idiom.** It mentions `capability_reverse_index`
   exactly once, in passing, inside the collision-reporting bullet list — there is no line
   telling an agent *"to answer 'what else might this change break?', look up
   `capability_reverse_index[<global_id>].referencing_project_ids`"*. This is real, but it is
   **M6's file and M6's scope** by the plan's own text (line 53: "更新 SKILL.md 让 agent 知道先查
   这个再动手做重复的事"), and this track was instructed not to touch the other track's files.
   Flagging it here so the M6 track picks it up rather than it falling between the two.

---

## 9. Bottom line

| Question | Answer |
|---|---|
| Is M5 already satisfied by M4's deployed output? | **Yes, completely.** |
| Any code written or file modified? | **No.** This note is the only file created. |
| `build_cross_project_catalog.py` | Unmodified — `8ffb5822c30e0a1d10264640678aff065691163f74b77071560704bb57bd5545` |
| `test_build_cross_project_catalog.py` | Unmodified — `ef496bea7c9df31c53dd519860f00dd9c6f426d340b1c8955b9ac4f1a1929526`; 100/100 green |
| Deployed `catalog.json` | Unmodified — `e1e3fec820564129856de46d4efb7c78c18c8e024307bc967464b4099b0fabc8` |
| Trust anchor `build_startup_bundle.py` | **Unchanged** — `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` (verified at start and at end) |
| Committed / deployed to `~/.agents/skills/`? | **No** to both. |
| `text_mention` (M8) | Not implemented, not started, untouched. |
