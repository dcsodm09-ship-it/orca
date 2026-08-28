# M6 candidate fixes — round 2 (2026-08-22)

Addresses the independent review's 2 P2s and 2 of its 3 P3s (P3-2, the lone-surrogate
encoding edge case, is deliberately deferred — see below). Also fixes an unrelated
deployment gap found by the same review cycle in M4's SKILL.md deploy (see below).

## Fixes applied to `query_catalog.py`

| Finding | Fix |
|---|---|
| **P2-1** — `verified_at` was the one catalog-sourced string reaching the human-output header line unsanitized (every sibling string goes through `_flatten_for_terminal()`); a hand-crafted catalog could inject raw newlines/ESC and forge an extra result row. | `_print_human()`'s header line now flattens `result['verified_at']` the same way every other field does. |
| **P2-2** — `global_id`/`project_id` were not searchable at all, so feeding the tool's own printed `global_id` back into it (the natural copy-a-hit round trip) produced a false "nothing in the catalog uses that word". | Added `project_id`/`global_id` to `CAPABILITY_IDENTITY_FIELDS` and `PAGE_IDENTITY_FIELDS`. Also reworded the no-match message to state exactly which fields were searched (id/name/title/summary/project_id/global_id) rather than the broader, now-still-slightly-imprecise "nothing in the catalog uses that word". |
| **P3-1** — `--stale-after-hours inf` passed the `> 0` gate (`inf > 0` is `True`) and blew up later converting to `int`, surfacing as exit 4 ("could not read the catalog") for what is a usage error. | `positive_number()` now also requires `math.isfinite()`; exits 2 via argparse, not 4. |
| **P3-3** — Two docstrings (`QueryFatal`, `search_catalog()`) claimed no exception is possible once a catalog is loaded; `search_catalog()` itself raises `QueryFatal("catalog_malformed")` when the parsed JSON isn't shaped like a catalog. | Both docstrings corrected to name the real exception path, with an explicit warning aimed at M7's planned SessionStart caller: wrap the call in `try/except QueryFatal`, don't assume "loaded" means "cannot raise". |

**Deliberately not fixed — P3-2** (a lone UTF-16 surrogate in a catalog string can make `print()` raise `UnicodeEncodeError` after partial human-output has already been printed, leaving stdout and the exit code telling different stories). Reachability requires a hand-crafted catalog — the real aggregator's own `.encode("utf-8")` call would reject a lone surrogate before it could ever reach `catalog.json`. Fixing it properly means deciding a policy for the whole print boundary (replace/escape late-discovered bad codepoints), which is a slightly bigger design decision than the other three one-line fixes; flagged for a follow-up rather than rushed here.

## Also fixed — M4 deployment gap found during this review cycle

The review's synthesis independently found that the M4 deploy earlier today left the live
`~/.agents/skills/orca-context-bridge/SKILL.md` missing a `### Re-signing
reviewed-startup-pack-manifest.json` subsection that had separately been committed to git
(`c756c85dbc`, 17:51:30) — a real, already-in-use, already-proven-correct piece of manual-
procedure documentation (this exact session used it minutes earlier to re-sign the manifest
during M4's own deploy). It was never live before M4's deploy either (confirmed against the
M4 deploy backup), so nothing that was previously live got removed — but it should have been
carried forward as part of closing that gap, and wasn't. Redeployed
`~/.agents/skills/orca-context-bridge/SKILL.md` to include it, while continuing to exclude
M6's own not-yet-approved "Searching the catalog" subsection until M6 clears its own gate.
New deployed hash: `464c39d3c511732de45fccdefd7b2d721ed4afe262674f7f89a34ee91b9d9c03` (matches
the already-reviewed M4/grok-round candidate's SKILL.md content exactly — no new, unreviewed
prose was introduced).

## Re-verification

- `python3 -m py_compile` clean on both files.
- Full `test_query_catalog.py` suite: **55/55 pass** (was 53; +2 new tests, 2 existing tests
  updated for the new correct behavior — one fixture rewritten so it isolates id/name/summary
  matching from the new project_id/global_id matching, one import-surface pin updated for the
  new `math` import).
- Sibling `test_build_cross_project_catalog.py` suite still **100/100 pass** — unaffected.
- Real queries against the live catalog confirmed: searching the tool's own printed
  `global_id` and `project_id` now finds the entry; `--stale-after-hours inf` now exits 2.
- Trust anchor `~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py` —
  `50721e76835ed9e162577fa66757f129aa195a33e5d37e96436ab84a8dceccbe` — confirmed unchanged.

## Final candidate hashes (this round)

```
4563b4ed918f72024d21a6a7397d0eb9254f8be8dff8c932cfc629b695bdb70e  orca-context-bridge/scripts/query_catalog.py
8bae9d8320a119dbf4d3295643694a39a1c399ce0cef0d8cb393ccef04bce330  orca-context-bridge/scripts/test_query_catalog.py
```

Not committed. `query_catalog.py`/`test_query_catalog.py` not deployed to `~/.agents/skills/`
(pending final gate). SKILL.md's M6 "Searching the catalog" subsection likewise stays
undeployed pending the same gate.
