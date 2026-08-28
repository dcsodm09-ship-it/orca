# M4 candidate — Grok-round fixes (2026-08-22)

## Context

After 3 rounds of independent Claude opus/max review (16 findings → 12 → 9, converging to
P0=0/P1=0/P2=0 with only doc-only P3s, all fixed), the standing rule (CLAUDE.md §4) calls
for one Codex `sol`+`max` final-gate review before this candidate is considered complete.
That leg failed twice with OpenAI's Trusted Access cybersecurity-content wall (not a
candidate defect — see `reference_codex_trusted_access_stall_diagnosis.md`).

Per explicit user request, two additional independent passes were added on top of the
standing process:

- **Grok** (`grok-4.6-build`, via the Orca-integrated `grok` CLI, dispatched through an
  Orca-tracked terminal since Orca has no native "grok worker" orchestration primitive —
  a real launcher gap, recorded rather than worked around silently).
- **Low-tier Codex** (`codex-bulk`, `gpt-5.3-codex-spark`, independent quota bucket) as a
  diagnostic to check whether the Trusted Access wall was tier-specific. It was: bulk
  completed cleanly with a GO and no Trusted Access block.

## Grok's verdict

**GO** (P0=0, P1=0, P2=0), independently re-derived (hashes, `py_compile`, full 97-test
suite, a live-fleet build with `--output` redirected off the production path, manual
escape/dedup/TOCTOU probes). Grok's own reasoning trace showed it raising and then
retracting several hypothesized P1/P2 findings (hardlink bypass, an ID-length exploit, a
confused-deputy symlink claim) before settling on its final answer — normal exploratory
noise for this kind of task, not evidence the final answer is unreliable, but exactly the
kind of report this project's discipline says must be independently re-verified rather
than trusted at face value. All 3 of its final P3 findings were re-derived by hand against
the actual code (not just read) and confirmed real:

## Fixes applied

| Finding | File:area | Fix |
|---|---|---|
| **P3-1** — `orca-context-wiki.json`'s `links` field, when present but not a list, was silently coerced to `[]` with the source still reported `"ok"` — the exact "wrong type reported as healthy" shape `pages` is correctly treated as `schema-invalid` for, just not mirrored onto `links`. | `summarize_context_wiki()` | Added `links_malformed` (bool, per wiki source) and a new top-level `counts.projects_with_malformed_links`. A malformed `links` now degrades the source to `"partial"` with a reason; absence of the key (the normal case) stays silent. |
| **P3-2** — a non-string (or empty-string) `depends_on` entry was silently `continue`-d past with no trace anywhere — real information loss, unlike the adjacent lossless raw-string dedup (R3), which *is* recorded. | `summarize_reusable_capabilities()` | Added a per-capability `malformed_depends_on_count` and a new top-level `counts.malformed_depends_on_entries`, aggregated separately from `dropped_count` (which specifically means "capability objects removed from `caps[]`" and is an invariant the test suite pins — folding this in would have broken `capability_count + dropped_count == len(capabilities_raw)`). Escalates the source to `"partial"` when non-zero. |
| **P3-3** — `ID_RE` is imported from the M3 validator and enforced, but `ID_MAX_LEN` (64) is not: `ID_RE` alone has no upper bound on length, so a 65+ character capability `id` matched the regex and minted a publishable `global_id` that the validator's own hard length check would reject — an id this aggregator accepted but the validator calls dirty, the exact drift the shared-grammar import exists to prevent. | import line + the `id` gate in `summarize_reusable_capabilities()` | `ID_MAX_LEN` is now imported alongside `ID_RE`; the gate is `not (1 <= len(cid) <= ID_MAX_LEN) or not ID_RE.match(cid)`, mirroring the validator's own check exactly. |

One pre-existing test (`test_p2_2_module_sets_dont_write_bytecode`) asserted the literal
text of the import line; updated to match the new `ID_MAX_LEN` import. Three new tests
added (`test_grok_p3_capability_id_over_id_max_len_is_dropped`,
`test_grok_p3_non_string_depends_on_entry_is_counted_not_silent`,
`test_grok_p3_links_not_a_list_degrades_instead_of_silent_empty`).

## Re-verification performed

- `python3 -m py_compile` clean on both files.
- Full suite: **100/100 pass** (was 97; +3 new).
- Trust anchor `~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py` —
  sha256 `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe`, confirmed
  unchanged before and after.
- Fresh live-fleet build (`--output` redirected to scratch, production path untouched):
  exit 0, `degraded_count: 0`, 146 scan targets, 17 capabilities, 43 pages; new counters
  (`malformed_depends_on_entries`, `projects_with_malformed_links`) both correctly `0` on
  real data (the real fleet has no malformed shapes to trigger them).
- Both pilot files re-validated: `{"ok": true, "errors": [], "warnings": []}`
  (`orca/完善orca` 12 caps, `rn邮箱` 5 caps) — byte-identical, untouched by this round.
- `git status`: both candidate files still untracked (`??`); nothing committed, nothing
  deployed to `~/.agents/skills/`.

## Final candidate hashes (this round)

```
8ffb5822c30e0a1d10264640678aff065691163f74b77071560704bb57bd5545  orca-context-bridge/scripts/build_cross_project_catalog.py
ef496bea7c9df31c53dd519860f00dd9c6f426d340b1c8955b9ac4f1a1929526  orca-context-bridge/scripts/test_build_cross_project_catalog.py
```

`SKILL.md`'s M4 section is unchanged by this round (sha256 `464c39d3c511732de45fccdefd7b2d721ed4afe262674f7f89a34ee91b9d9c03`, same as round 3).

## Status

Four independent review passes now agree: **P0=0, P1=0, P2=0** on the current candidate
(3× Claude opus/max, 1× Grok, plus a non-exhaustive low-tier Codex sanity pass that also
found nothing). The only outstanding item against the standing CLAUDE.md §4 process is the
Codex `sol`+`max` final-gate leg specifically, blocked twice by Trusted Access on the only
Codex account configured on this machine — a human decision (request Trusted Access for
this account, retry later, or accept the Grok pass as this gate's substitute this time) is
needed to close that out, not a code fix.

Fleet counts above are a live moving target in this actively multi-session repo — re-derive
via `orca repo list --json` / `orca worktree list --json` at review time rather than diffing
against the literals here.
