# M6 candidate fixes — round 3 (2026-08-22)

Both the Codex `sol`+`max` final gate and an independent Grok pass gave **GO** (0 P0/P1)
on the round-2 candidate. Both converged on several of the same real P2/P3 findings
(cross-validated, not a single reviewer's opinion), plus Codex found two more on its own.
None were required to reach GO — the standing rule only blocks on P0/P1 — but they're real,
cheap, and match this project's established pattern of fixing what's found rather than
letting it sit. Fixed here rather than left for a later round.

## Fixes applied

| Finding | Source | Fix |
|---|---|---|
| A hand-crafted `--stale-after-hours 1e308` is finite (passes `positive_number`'s check) but overflows to `inf` after `× 3600`, then crashes `int(inf)` inside `search_catalog()` as an `unexpected_error` (exit 4) — the wrong bucket for a bad CLI argument. | Both Codex and Grok, independently | Finiteness re-checked in `cmd_search()` **after** the multiplication, as a usage error (exit 2, reason `stale_after_hours_overflow`). Also defensively guarded a second time inside `search_catalog()` itself (a non-finite `stale_after_seconds` now clamps to a huge-but-valid int instead of raising) since that function's own docstring promises no exception beyond catalog loading, and it is public — a future library caller (M7) could reach it directly, bypassing the CLI-layer guard. |
| A catalog containing the non-standard JSON tokens `NaN`/`Infinity`/`-Infinity` (Python's `json.loads` accepts them; the real aggregator's `json.dumps` can never emit them) flowed through into this tool's own `--json` output as a bare, RFC-8259-invalid literal a strict downstream parser (e.g. `JSON.parse`) rejects. | Both Codex and Grok, independently | `json.loads(..., parse_constant=_reject_non_finite_constant)` — same severity call as the existing `_reject_duplicate_keys()`: refused outright as `catalog_unparseable` (exit 4), not silently laundered through. |
| `--help` and the module docstring's RANKING section still only named `id`/`name`/`title`/`summary` as searchable, one fix-round behind the actual code (which already searches `project_id`/`global_id` too, per round 2's fix). | Both Codex and Grok, independently | Module docstring's opening paragraph updated to name all six fields and explain why `global_id`/`project_id` were added. |
| A `verified_at` **ahead of** the clock (year 9999 in the reviewer's repro) got a warning but was NOT marked stale — exactly as unprovable as a missing or unparseable timestamp (which already count as stale), so a caller checking only the `stale` boolean could trust an answer this function cannot actually vouch for. | Codex | `stale = True` whenever `age_seconds < 0`, matching the existing "unprovable freshness must never read as proven freshness" rule already applied to the missing/unparseable cases. |
| A zero-match result where `capabilities[]` or `wiki_pages[]` was absent/malformed (so only the other list was actually searched) was exit 1 — identical to a genuinely complete search finding nothing, and `--quiet` (the mode meant for exit-code-only callers) additionally suppresses the one place the difference was otherwise visible (the warning text). For a tool whose entire purpose is "does this already exist?", that conflation is exactly the silent-false-negative shape the rest of this tool's design refuses to allow. | Codex | New exit code **3**: "no match, but the search was partial." Exit 1 stays "no match, and the search was complete." Exit 0/2/4 unchanged. Documented in the module docstring's EXIT CODES section and `--help`. |

## Deliberately not fixed (documented, not silently dropped)

- **Lone UTF-16 surrogate in a catalog string crashing `print()` mid-output** — both reviewers re-confirmed this is real but unreachable from the real aggregator's own output (its UTF-8 encode-to-write step rejects a lone surrogate before it could ever reach `catalog.json`); fixing it means picking a policy for the whole print boundary (replace vs. escape late-discovered bad codepoints), a slightly bigger decision than the fixes above. Same call as round 2.
- **Non-UTF-8 stdout environments** (`PYTHONIOENCODING=ascii` crashing on CJK output) and **`BrokenPipeError` producing an undocumented exit 120** when a downstream reader (e.g. `| head`) closes early — both are narrow, environment-specific edge cases bundled with the surrogate item above as one future output-boundary-policy decision rather than three separate patches.
- **Python 3.9 vs 3.14 giving different (but both non-crashing, both exit-4) reason codes for a pathologically deep JSON nesting bomb** (2000+ levels) — cosmetic inconsistency, requires adversarial input no hand-maintained catalog would ever contain, genuinely P4.
- **`_flatten_for_terminal()` leaves LRM/RLM/ALM directional marks** (weaker bidi-affecting characters than the RLO/PDF block it already strips) — real but far lower-severity than what's already handled, P4.

## Re-verification

- `python3 -m py_compile` clean.
- Full `test_query_catalog.py` suite: **58/58 pass** (was 55; +3 new tests: exit-3 partial-search behavior including its `--quiet` interaction and the "still exit 0/1 in the non-partial cases" contrast, the NaN/Infinity rejection across all three tokens, the stale-after-hours overflow-after-multiplication usage error). One existing test (`test_t25`) updated for the corrected future-timestamp-is-stale behavior, including an added case for a year-9999 timestamp specifically (the reviewer's own repro shape).
- Sibling `test_build_cross_project_catalog.py` suite still **100/100 pass** — unaffected.
- Real smoke tests: a hand-crafted `NaN` catalog now correctly exits 4 `catalog_unparseable`; `--stale-after-hours 1e308` now correctly exits 2 `stale_after_hours_overflow`; a year-9999 `verified_at` now correctly reports `STALE`.
- Trust anchor `~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py` — `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` — confirmed unchanged.

## Final candidate hashes (this round)

```
b04af3b3ff362d2b1dd25af29ff2fa0184d7db91494f96dcf1e93025aa8d4146  orca-context-bridge/scripts/query_catalog.py
2fdf76c1ea4b34ed812eb487504a9c442756c96316d8031c3c7a326ec01c356a  orca-context-bridge/scripts/test_query_catalog.py
```

Not committed. Not deployed to `~/.agents/skills/`. Both the Codex and Grok final gates
already returned GO against the round-2 bytes (0 P0/P1) before these hardening fixes were
applied on top; none of the fixes here touch a P0/P1-severity behavior, so a third external
gate round was judged unnecessary — re-verification above was done directly. SKILL.md's M6
"Searching the catalog" subsection is unaffected by this round and remains undeployed pending
a deployment decision for the whole M6 deliverable.
