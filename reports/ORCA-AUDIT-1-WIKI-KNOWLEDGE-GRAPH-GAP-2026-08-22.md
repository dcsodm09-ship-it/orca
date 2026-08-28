# 审计 1：wiki/知识图谱设计 vs 实际代码差距 + 实施计划

## Verification against current repo state (2026-08-22)

I re-read the design in full (including Appendix A) and independently re-derived every factual claim it makes against the real files — not trusting its own "current state" section. Result: **the design has not drifted at all.** Every line-numbered claim, every function signature, every "this symbol doesn't exist" assertion checked out exactly:

- `orca-context-bridge/scripts/build_knowledge_graph.py`: `GraphBuilder.__init__` (line 174) takes only `home`, no `generated_at`; `self.nodes` is a dict (176); `add_node` (186) has no `timestamps` param; `add_sessions`/`add_github` (405/518) don't take `sources`; `add_reviewed_manifest`'s binding loop (375-378) does `expected == observed` string equality; `GRAPH_VERSION = 2` (20). All exactly as the design's diffs assume.
- `wiki/orca-context-wiki.json`: 14 pages, 0 links have timestamps, no `meta` object, zero git history for the file itself.
- `orca-context-bridge/scripts/build_startup_bundle.py`: `REVIEWED_PACK_MANIFEST_SCHEMA_VERSIONS = {3, 4}` (78-82); `verify_reviewed_pack`'s per-source loop is at lines 1417-1447, and line 1443 is exactly `if expected is not None and not valid_sha256(expected):` as cited.
- `.orca/context/reviewed-startup-pack-manifest.json`: still `schema_version: 2` with `authority_signed_at`/`signed_by`/`signature_nonce`, still fails the current code's strict field-set check — the v2/v3 gap the design calls Risk 5 is still live, unresolved.
- `orca-context-bridge/reviewed-pack-multi-project-fix/sign_reviewed_authority.py`: still the same unwired prototype, comment intact ("NOT called automatically by SessionStart hooks").
- `build_context_digest.py`: `Session` dataclass (118-128) still has no `path` field; `parse_timestamp` (139-155) still never raises, returns `None` on bad input.
- `sync_sessions.py`: `validate_catalog_session`'s `expected` set (749-753) matches verbatim; no `ALLOWED_CATALOG_SESSION_KEYS` symbol anywhere; `opaque_codex_account_source` (707) and the `codex_home` assignment (991) match exactly.
- `wiki_edit_guard.py` / `check_wiki_freshness.py`: confirmed absent — genuinely not implemented, as the task states.
- The Audit 1–4 / candidate / critique files the design's header links to **already exist** in `reports/` — so Risk 8 ("audits not persisted to repo") is already resolved, not an open item.

So this is not a "design vs. drifted reality" reconciliation — it's a straight "go build it" situation, plus two things I found that the design didn't have visibility into.

## Two things not in the design (found by checking real consumers/tests)

1. **A concrete downstream consumer for Risk 4's abstract warning.** `orca-context-bridge/scripts/context_route.py:26` hardcodes `SUPPORTED_GRAPH_VERSIONS = {2}` and fails closed (`RouteError("knowledge graph version is unsupported; rebuild the index")`) at `context_route.py:170` if the graph's `version` isn't in that set. Bumping `GRAPH_VERSION` to 3 without touching this line breaks all route resolution the moment the graph is rebuilt. Also `tests/test_knowledge_graph.py:224` hardcodes `assertEqual(graph["version"], 2)`. Both must change in the same commit as the `GRAPH_VERSION` bump.
2. **The write-barrier has nothing to be a barrier to yet.** `wiki/orca-context-wiki.json` has zero git history and no script currently writes it — it's hand-edited. So `wiki_edit_guard.py` as designed is a tool nobody is forced to call unless something wires it in (git pre-commit hook, or a hard requirement in whatever agent/human workflow edits this file). Shipping the guard script without an enforcement point reproduces exactly the "detection exists, gap isn't actually closed" failure the design itself indicts its predecessor for (§2.2, opening paragraph). This needs an explicit step, not an assumption.

## Design content that is now stale (Phase B drafts only — structure is unaffected)

The design's §4.1 already anticipated a `prime-agent-integration` page (item 5) and scoped `claude-codex-memory-bridge` (item 4) to include `write_candidate_capture.py` — so nothing is *missing* from the design's own list. But their **draft summary text is now out of date**, since this repo has done 5 more rounds of work since 2026-08-20 (design commit `047d54312b`):

- Item 5 draft says "最终双模型双 GO，但因上游依赖漂移目前仍未安装（阻塞态）" — true as of round ~48. Reality now (rounds 49-53, commits `b00e9a6f81`…`7fd3df39a5`): round 52 reached "first genuine dual-GO of the whole 52-round cycle," then the round-53 re-pin review found a real unpatched HIGH-severity advisory, a major-version drift, and a ~2-day pin half-life. Still blocked, still not installed, but the story is materially different and more specific now.
- Item 4 draft says "已实际安装生效" — true only for the base `install_bridge.py`/`claude_memory_hook.py` bridge (installed 2026-08-19/20 per user memory). The `write_candidate_capture.py` write-trigger extension this same page is scoped to cover is a **separate, still-NO-GO** track (`OPUS5-INDEPENDENT-REVIEW-install-bridge-ROUND53-2026-08-21.md`: P1, uncaught `ImportError` bricks the uninstall/recovery path — a real lockout bug, not yet fixed as of the latest review I read).

Action: whoever executes Phase B must re-derive both summaries from the latest reports at write time, not copy the design's draft text verbatim. This also argues for including an explicit "installed base vs. pending extension" status split within the `claude-codex-memory-bridge` page rather than one blended summary.

## Ordered implementation plan

**Phase A — schema/infrastructure only, no new page content (per design's own Risk 3 mandate to keep this separate from Phase B).**

Track 1 (wiki + graph timestamps — no dependency on the manifest issue, can start immediately):

1. Write `wiki_edit_guard.py`: `guard_wiki_write()`, `compute_semantic_diff()`, `validate_link_verified_at_derivation()` per §2.2.2, using the existing `write_private()` atomic-write pattern (`build_context_digest.py:1297`) for the actual commit. Include the one-time "old payload has no `meta` key" bootstrap exception from §3.2.
2. Write the one-off migration script per §3.2 (`source_mtime` via `os.stat`; `created_at`/`updated_at` from git log only for `agent_capacity.py`, migration-timestamp for the other 13; `verified_at: null` + `verification_status: "unverified"` for all; `migration_sequence` = original array index; `meta.content_version = 1`), route its write through step 1's guard, run it once against `wiki/orca-context-wiki.json`.
3. Decide and implement the enforcement point for the guard (git pre-commit hook is the natural fit given no wiki-writing script exists yet) — otherwise it's opt-in only. Document this explicitly since it's a real gap the design didn't resolve.
4. `build_knowledge_graph.py` changes, in this order since each depends on the last:
   - Add `make_timestamps()` helper (with the `parse_timestamp`-returns-`None`-not-raises fix already spelled out in §5.2).
   - Thread `generated_at` through `run_build()` → `build_graph()` → `GraphBuilder.__init__` (single value computed once, used for both the top-level field and every node's `observed_at`).
   - Add `timestamps` param to `add_node`; capture `source_mtime_ns` in `load_regular_source`'s `record` dict (already computed as `after.st_mtime_ns`, just not stored).
   - Update `add_sessions`/`add_github` to accept `sources` and call `make_timestamps`; **fold in Appendix A's `record_path`/`record_on_ssd` meta fields into `add_sessions` in the same diff**, since it's the same function region (see Track 3 below for the prerequisite work this needs).
   - Fix `add_reviewed_manifest`'s binding loop (`expected_hash = expected.get("sha256") if isinstance(expected, dict) else expected`) — do this now even though the manifest is still v2/string-shaped; it's backward compatible and removes one blocking dependency from Track 2.
   - Add null-timestamp calls to the remaining node types (`wiki`, `project`, `pane`, `terminal`, `agent_process`, `capability`, `code_graph`, `route`, `skill`) and `source_mtime` to `sources[name]`.
   - Bump `GRAPH_VERSION` to 3.
5. Update the two now-identified downstream consumers in the same commit: `context_route.py:26` (`SUPPORTED_GRAPH_VERSIONS = {2, 3}`) and `tests/test_knowledge_graph.py:224` (`assertEqual(graph["version"], 3)`), plus grep once more repo-wide for any other `"version"] == 2` / `signed_at` reads before merging (Risk 4's own instruction).
6. Add tests: `make_timestamps` edge cases from Risk 6 (`None`, empty string, garbage string, tz-naive, `Z`, `+00:00`, numeric epoch); the `add_reviewed_manifest` dict-vs-string binding regression test.

Track 3 (Appendix A session indexing — do before finishing step 4's `add_sessions`, since it touches the same function):

7. Add `path: Path | None = None` to the `Session` dataclass (`build_context_digest.py:118`); pass `path=path` in both `return Session(...)` call sites in `parse_claude_session`/`parse_codex_session`.
8. Add `record_path`/`record_on_ssd` to `sync_sessions.py::validate_catalog_session`'s `expected` set and validation body; add `_is_under_ssd_root()` (using `Path.is_relative_to`, not string prefix — Risk 12) and `_sanitize_account_uuid()` helpers; wire both into `build_catalog()` alongside the existing `codex_home` assignment.
9. Handle the two correctness bugs Appendix A found in the existing (pre-this-design) code, since they become newly *observable* once `record_path` exists: Risk 10 (cross-source duplicate session-id indexing via account-pool symlinks) and Risk 11 (`.codex-profiles/acct-c`'s silent skip due to missing `sessions/` dir) — at minimum, log/flag both explicitly per the design's mitigation text; don't ship silent.
10. Tests: the six `record_on_ssd` scenarios in Risk 9 (real vs. symlinked account homes), the decoy-prefix case in Risk 12, and the duplicate-session-id case in Risk 10.

Track 2 (manifest — gated, do in parallel but do not merge/deploy ahead of Track 1):

11. **Prerequisite investigation (blocking, independent task, not really "this design's" work but gates it):** determine why the live manifest is still schema_version 2 against code that's required v3/v4 since before this design existed — is there an in-flight migration, a forgotten cutover, or is `sign_reviewed_authority.py` genuinely the only (unwired) generator? Confirm whether the "non-self-signed, ≥300s aging" invariant is still active policy anywhere. This is Risk 5, and it's still open exactly as the design says.
12. Once resolved: regenerate the live manifest as schema_version 3 (or 4) with real current pack/hash contents; only then apply §2.2.3's `shared_source_sha256s.wiki` object-ification diff on top of it.
13. Apply the `verify_reviewed_pack` diff from §2.2.4 (new/old format branch, `content_version` fail-closed on missing/wrong-type, the `isinstance(x, int) and not isinstance(x, bool)` guard). Write the four required fail-closed tests from Risk 1 (hash-ok/version-bad, hash-bad/version-ok, both-missing, version-wrong-type).
14. Write `check_wiki_freshness.py` (§2.2.5), reusing the per-source check logic from step 13 rather than duplicating it.

15. **Mandatory dual review before merge/install**, per the user's global CLAUDE.md rule (this is exactly a "安全/发布/最终验收" case — it touches `verify_reviewed_pack`, the sole live fail-closed authority check): Claude opus+max and Codex gpt-5.6-sol+max independent read-only review of steps 11-14 together. Any reproducible P0/P1 on either leg blocks merge; re-review after any fix.
16. Confirm no other repo-wide `GRAPH_VERSION`/`signed_at`/manifest-string-shape readers were missed (final Risk 4 sweep) before deploying Track 1+2 together.

**Phase B — content, strictly separate commit/review from Phase A (Risk 3), starts only after Phase A is merged and stable:**

17. Re-run the stale probes (`orca-capability-tests`, `orca-readonly-probe`, `orca-cli-capability-catalog`) to get real current numbers before writing anything — don't reuse the design's cited "16/18"/"235" figures without re-verifying, since the design itself says these must be verified-at-bound, not hardcoded.
18. Write the 14 new pages from §4.1 in priority order (P0 → P1 → P2), applying the write-barrier from Track 1. For items 4 and 5 specifically, discard the design's draft summary text and write fresh from the current state described above (round 53 status for prime-agent-integration; installed-base-vs-pending-extension split for claude-codex-memory-bridge).
19. Apply the 7 existing-page updates from §4.2 (`resource-gate`, `capacity-preflight`, the three probe pages, `desktop-mcp-realtime-control`, `orchestration-lifecycle`/`resource-gate` cross-links, `ego-capability-regression`).
20. Each page moves from `verification_status: "unverified"` to `"verified"` only when someone has actually re-checked it against current source — not automatically on creation.

Files touched, for reference: `orca-context-bridge/scripts/build_knowledge_graph.py`, `orca-context-bridge/scripts/context_route.py`, `orca-context-bridge/scripts/build_context_digest.py`, `orca-context-bridge/scripts/sync_sessions.py`, `orca-context-bridge/scripts/build_startup_bundle.py`, `.orca/context/reviewed-startup-pack-manifest.json`, `wiki/orca-context-wiki.json`, `tests/test_knowledge_graph.py`, plus two new scripts (`orca-context-bridge/scripts/wiki_edit_guard.py`, `orca-context-bridge/scripts/check_wiki_freshness.py`) and a one-off migration script.