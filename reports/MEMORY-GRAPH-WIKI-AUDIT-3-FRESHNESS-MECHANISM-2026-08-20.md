# Audit 3: 既有新鲜度/哈希钉定机制

## Findings: Wiki freshness/staleness check mechanism (ORCA_CONTEXT_NACK_V1)

### (a) String search results
- `ORCA_CONTEXT_NACK_V1`: appears only as a literal emitted at `orca-context-bridge/scripts/startup_context.py:863` (inside `cmd_hook`'s exception handler), plus many prior-session review reports (`CODEX-SOL-*.md`, `OPUS5-*.md`) that recorded receiving it at startup.
- `"central reviewed source ... freshness mismatch"`: the exact raise site is `orca-context-bridge/scripts/build_startup_bundle.py:1446` — `raise ValueError(f"central reviewed source freshness mismatch: {name}")`. Same string (with slight schema variants) also exists in three parallel/candidate copies of this function: `orca-context-bridge/security-fixes/build_startup_bundle_fixed.py:1383`, `orca-context-bridge/reviewed-pack-multi-project-fix/build_startup_bundle_fixed.py:1376`, `orca-context-bridge/reviewed-pack-multi-project-fix/build_startup_bundle_from_prod.py:637` — these are staged/candidate variants, not the live imported module.
- A captured instance of this exact NACK reason (`"reason": "central reviewed source freshness mismatch: wiki"`) is recorded as data in `orca-context-bridge/routes/orca-context-routing-v2.candidate-manifest.json:285`.
- `"freshness"` as a *field name* (`shared_freshness`) appears in `startup_context.py` at lines 95, 519, 524, 698, 1029, 1297 — always as a hash bundle, never a time value (see part b).

### (b) The manifest
Read `.orca/context/reviewed-startup-pack-manifest.json` (live, untracked by git — confirmed via `git ls-files`, zero results):
```json
{
  "schema_version": 2,
  "authority": "orca-central-reviewed-l1-l3",
  "authority_signed_at": "2026-08-14T07:28:05.245815+00:00",
  "signed_by": "dcsodm09-ship-it-manual-review-2026-08-14-authority-scope-fix",
  "shared_source_sha256s": {
    "capabilities": "13758f19...fae59",
    "graphify_catalog": "7454e66b...09ef",
    "wiki": "ff93b6d7...9d02"
  },
  ...
}
```
It pins one SHA-256 per shared source (capabilities file, Graphify catalog, wiki JSON) plus a Git content-identity block. `authority_signed_at`/`signed_by`/`signature_nonce` are produced by the manual signing tool `orca-context-bridge/reviewed-pack-multi-project-fix/sign_reviewed_authority.py:104,121` — **but these fields are audit metadata only**: they are outside the exact key-set the live verifier accepts (see below), so they are never read by the comparison logic itself.

**Side finding (corroborates a note already in the backlog report, not new):** this on-disk manifest is `schema_version: 2`, but the live `build_startup_bundle.py:78-82` only accepts `REVIEWED_PACK_MANIFEST_SCHEMA_VERSIONS = {3, 4}`, and its schema check at `build_startup_bundle.py:1356-1362` requires the exact key set `{schema_version, authority, pack, shared_source_sha256s, shared_source_policy, project_git, content_source_closure}` — a set this manifest doesn't satisfy (it has `authority_git`/`authority_signed_at`/`signed_by`/`signature_nonce` instead of `content_source_closure`). This matches the backlog's already-recorded "installed hook lags repo (schema v2 vs v3/v4)" side finding.

### (c) The three scripts
- **`startup_context.py`**: `current_shared_state()` (lines 488-516) computes `git`/`graphify` state and calls `verify_reviewed_pack(...)` (imported from `build_startup_bundle.py`) at line 503, passing the exact shared-source paths (line 508-513, including `knowledge_root / "wiki" / "orca-context-wiki.json"` at line 510). Any `ValueError` raised inside propagates up through `refresh_and_build` → `cmd_hook` (line 841) and is caught at lines 860-869, which formats `ORCA_CONTEXT_NACK_V1` with `reason=<exception message>` (line 866) — this is the literal end-to-end path that produces `reason=central reviewed source freshness mismatch: wiki`.
- **`install_startup_injection.py`**: installs the SessionStart hook that invokes `startup_context.py cmd_hook`; it does not itself contain freshness/hash comparison logic — confirmed no `stale`/`freshness`/hash-compare code found there.
- **`sync_sessions.py`**: unrelated to this mechanism — it indexes local Claude/Codex session catalogs, not the reviewed-pack manifest.

### (d) Exactly how "stale" vs "fresh" is decided today — and whether there's a time dimension
`build_startup_bundle.py:1416-1447` (inside `verify_reviewed_pack`):
```python
for name in reviewed_shared_sources:          # "capabilities", "wiki", "graphify_catalog" (+"routes" in schema 4)
    ...
    observed_file = stable_regular_file_sha256(checked_source, MAX_GRAPHIFY_CATALOG_BYTES)
    observed = observed_file[0] if observed_file is not None else None
    expected = expected_sources.get(name)
    ...
    if observed != expected:
        raise ValueError(f"central reviewed source freshness mismatch: {name}")   # line 1446
```
**This is a pure content-hash equality check. There is zero time dimension anywhere in this comparison.** No `mtime`, no `pinned_at`, no TTL, no age arithmetic is read or computed at this call site or anywhere in `verify_reviewed_pack`. The word "freshness" in this codebase is used as a *label for a bundle of hashes* (`freshness = {reviewed_manifest_sha256, reviewed_pack_sha256, shared_source_sha256s, project_git, ...}` at `build_startup_bundle.py:1480-1489`), not a timestamp comparison.

The only genuine time-based logic anywhere nearby is unrelated: `startup_context.py:53` defines `RECENT_BUNDLE_SECONDS = 30`, used at line 666 to let a hot-path re-invocation within the same 30-second window skip rebuilding the whole bundle — but even that path still re-derives `shared_freshness` (hashes) at line 693 and requires it to still equal the previous snapshot's `shared_freshness` (line 698) before reuse is allowed. So even the one TTL that exists in this code defers back to hash equality; it does not let a stale hash slide through on account of recency.

I verified empirically that the mismatch is real and current (as of 2026-08-20): live `wiki/orca-context-wiki.json` hashes to `538fd671d651a9381d1c7e18920ddfd631ce1cfae38cb69a431116dee7f17f1d`, but the manifest still pins `ff93b6d73f490affd2f8427c5feaaa16eb728040e61f1ecba1f8522f2f7e9d02`. The `capabilities` source, by contrast, still matches exactly (`13758f19...fae59` both places), confirming the mismatch is isolated to `wiki` as the reports claim. Filesystem mtimes: `wiki/orca-context-wiki.json` = 2026-08-15 01:35:14; manifest = 2026-08-14 15:28:05 — a ~10h07m gap, matching the reported "~10 hours" figure.

### (c/incident) Prior discussion found
- `reports/COMPLETION-BACKLOG-2026-08-16.md` lines 162-189 (section "6", "2026-08-18 补充" subsection) — full incident writeup: root cause is that `wiki/orca-context-wiki.json` was hand-edited ~10 hours after the manifest was signed, and nobody re-signed; the comparison logic itself is judged correct (not a bug); the fix was deliberately *not* applied without human content review + dual review, since the wiki is authored/reviewed content, not a script-regenerable artifact. It also names the exact hash the wiki *would* need to be re-pinned to (`538fd671d6...`), which matches what I computed live. It records the schema v2/v3-v4 install lag as a separate side finding.
- `reports/ORCA-COLLAB-STATE-AND-PRIME-AGENT-2026-08-18.md` — summary at lines 55-56, and **section 2** at lines 2418-2425 ("`ORCA_CONTEXT_NACK_V1`（wiki 新鲜度不匹配）根因") — a condensed pointer back to the COMPLETION-BACKLOG writeup, same conclusion. Section 5 (line 2460) explicitly lists "did not re-pin the wiki hash" as an intentional, not-yet-authorized omission (`BLOCKED_HUMAN_AUTH` + `BLOCKED_DUAL_REVIEW`).

Both reports agree: this is currently in an intentionally-unresolved `BLOCKED_HUMAN_AUTH`/`BLOCKED_DUAL_REVIEW` state, not something silently left broken.

### My assessment: what a real fix (not cosmetic) needs

The root problem isn't "no timestamp exists" — it's that **the pinning event and the content-edit event are two independent, uncoordinated actions with no enforced ordering or invalidation trigger between them.** A timestamp field alone doesn't fix that; it just gives you a number to *not check*. Concretely, a fix needs:

1. **An edit-time write barrier, not just a read-time timestamp.** Whatever process/editor writes `wiki/orca-context-wiki.json` must be the thing that stamps it (e.g., bump `wiki["version"]` — currently a static, unused `1` — and/or write an `updated_at` inside the wiki JSON itself, not rely on filesystem mtime, which is trivially reset by any copy/checkout/touch and isn't tamper-evident).
2. **The manifest must pin the wiki's own version/edit-marker, not just its byte hash**, so a human editing the wiki is *structurally required* to bump that marker as part of the edit — making "I edited it but forgot to re-sign" produce an immediate, local, edit-time-visible signal (e.g., a pre-commit/pre-save check comparing wiki's internal version against the manifest's pinned version) rather than only surfacing 10 hours later as a degraded agent startup.
3. **The comparison must stay hash-based for content integrity** (that part is correct and shouldn't be weakened — a time-only check would let an attacker touch-and-backdate around it). A timestamp/version should be an *additional* required-match field alongside the hash, not a replacement, and specifically not a wall-clock "is it within N hours" TTL — TTLs would either (a) be too short and constantly false-NACK on legitimate slow-moving wikis, or (b) be too long and silently accept a stale/compromised wiki for hours, which is strictly worse than today's fail-closed hash mismatch.
4. **The signal must reach the human before the next agent hits fail-closed**, not just after. Today the only place staleness surfaces is deep in a session-start hook exception message. A real fix wants a cheap, standalone freshness-check command/pre-commit hook runnable right after any wiki edit, so the ~10-hour gap between "edit landed" and "someone finally re-signs" shrinks to ~zero rather than being caught retroactively by whichever agent happens to start a session in between.
5. **Any timestamp addition should not touch `verify_reviewed_pack`'s existing hash-equality fail-closed behavior** (`build_startup_bundle.py:1445-1446`) — it's correctly strict today; the goal is upstream prevention (item 1-2), not downstream leniency.

Given both reports already flag this as `BLOCKED_HUMAN_AUTH` + `BLOCKED_DUAL_REVIEW`, any concrete fix to the manifest/wiki schema or the sign/verify scripts should go through the same human-review-then-opus+max/sol+max dual-review gate before being applied — consistent with the user's global CLAUDE.md rule on dual review for security/authority mechanisms.