# Audit 4: wiki 完整性缺口普查

## Wiki completeness survey — findings

**Scope read:** `wiki/orca-context-wiki.json` in full (14 pages, 24 links, all rooted under `orca-capability-tests`). Cross-checked against: 122 top-level entries in the repo, `orca-context-bridge/scripts/` (18 scripts) + its `SKILL.md`, and ~14 report files across `reports/`, `claude-codex-memory-bridge/`, `prime-agent-integration/`, and the `*-closure`/`*-independent-acceptance` directory family.

### Headline quantitative finding
Of ~35 substantive top-level directories/subsystems in this repo, only **2** have any wiki representation (`desktop-mcp`, and `orca-context-bridge` — and that one only for 4 of its 18 scripts). The wiki currently documents the *testing/verification layer* (capability probes, capacity gates, Ego policy) almost exclusively. It does **not** document any of the actual *engineering deliverables* — the memory bridge, Prime Agent integration, the SSD/R2/startup hardening closures, or the context-bridge pipeline's own core scripts — which is where the overwhelming majority of this repo's file count, commit history, and multi-round dual-review effort actually lives.

---

### (1) Candidate new wiki pages, ranked by centrality

1. **Startup context / reviewed-authority delivery pipeline** (`startup_context.py`, `install_startup_injection.py`, `.orca/context/reviewed-startup-pack-manifest.json`, the ACK challenge-response protocol) — this is the trust root every other subsystem depends on (`ORCA_CONTEXT_NACK_V1`, schema v3/v4 required sources, 300s ACK TTL). Currently only touched tangentially via `capacity-preflight`. Nothing explains what this pipeline *is* or how the manifest/pack/ACK pieces compose.

2. **claude-codex-memory-bridge** (`install_bridge.py`, `claude_memory_hook.py`, `write_candidate_capture.py`) — the single most-reviewed subsystem in the repo (15+ `ROUND` review files, a dedicated 2026-08-20 install-recommendation synthesis). Zero wiki presence despite being the largest body of review work here.

3. **prime-agent-integration** (`install_prime_agent.py`) — a 17-round security saga that found and closed a real RCE across 7 consecutive rounds, ending in genuine dual GO (Claude opus/max + Codex sol/xhigh) but still blocked from install by upstream npm drift. Zero wiki presence for a subsystem with this much security narrative.

4. **orca-context-bridge as a whole pipeline** (not fragments) — `sync_sessions.py`, `build_context_digest.py`, `index_processes.py`, `index_github.py`, `build_knowledge_graph.py`, `auto_index.py`, `install_hook.py`, `tmux_bridge.py` are all live, tested (each has a `tests/test_*.py`), described at length in `SKILL.md`, yet none has a wiki page. The wiki currently documents only the small side-probes (`agent_capacity.py`, `orca_readonly_probe.py`, `orca_lifecycle_precondition_probe.py`), not the pipeline's actual purpose.

5. **Route catalog / `context_route.py`** — a security-sensitive capability→project resolver (exact-hash source binding, `source_drift` fail-closed, candidate vs. reviewed query modes) described in detail in `SKILL.md` §"Capability-to-project routes" but absent from the wiki entirely.

6. **`reports/COMPLETION-BACKLOG-2026-08-16.md` as a master index** — this file is *already* the project's de facto table of contents, organized into 9 numbered domains (startup/hook injection, SSD native storage, R2/archive/Android retirement, Paperclip, memory/knowledge-graph, Prime Agent, desktop/browser, sibling worktrees). It is not linked from the wiki at all, even though it enumerates almost every other candidate on this list.

7. **SSD hardening closure family** (`ssd-native-storage-closure`, `ssd-runtime-closure`, `ssd-pty-lifetime-closure`, `ssd-runtime-remote-policy-independent-acceptance`) — a coherent multi-round effort to make Orca's native storage/PTY/runtime SSD-safe, comparable in size to the memory bridge. Zero wiki coverage.

8. **R2 backup/restore + archive/Android retirement closure family** (`r2-run-identity-closure`, `r2-production-trust-closure`, `r2-public-only-acceptance`, `archive-provenance-normalization-closure`, `android-retained-retirement-closure`) — a whole backup/retirement domain, all status `GO_OFFLINE_CANDIDATE_ONLY`, none installed, none in wiki.

9. **Startup installer/loader/transaction closure family** (`startup-installer-closure`, `startup-loader-closure`, `startup-transaction-closure`+v2, `startup-p1-hardlink-closure`, `startup-permission-remediation-closure`, `startup-reviewed-pack-closure`+schema3-fastfix) — the actual mechanism underneath item #1 (the installer that safely writes the reviewed pack / hook injection). Zero wiki coverage, despite being the machinery that produces the exact manifest the wiki's own hash is pinned into (see §2 below).

10. **`orchestration-dynamic-scheduler`** (`scheduler.py`, `ORCA-NATIVE-ARCHITECTURE.md`) — the dispatch-policy engine for multi-agent worker placement (account binding, Sol/Terra/Luna model routing, capacity gating). Directly overlaps with the wiki's existing `orchestration-lifecycle`/`resource-gate` pages but is never linked from them.

11. **Paperclip third-party integration family** (`paperclip-orca-closure`, `paperclip-privacy-gate-closure`, `paperclip-skill-candidate`, `paperclip-orca-independent-acceptance`) — another whole integration domain, zero wiki coverage.

12. **`skills/review-orca-workflow-learning`** — a full standalone meta-skill (a dozen scripts: `skilld.py`, `event_journal.py`, `telemetry_projection.py`, `memory_eval.py`, `startup_admission.py`, `policy_gate.py`, `attestation_verify.py`, `method_ledger.py`, `release_identity.py`, …) sitting alongside `orca-context-bridge` with no wiki page at all.

13. **The dual-review acceptance protocol itself** — `GO_OFFLINE_CANDIDATE_ONLY` / `NO-GO` / `ERRATUM` / round-numbering conventions recur across nearly every closure directory in the repo but are never documented as a first-class concept (only `codex-claude-bypass-dispatch` touches dispatch mechanics, not the review-gate vocabulary/lifecycle itself).

---

### (2) Existing pages that look stale or at risk

- **Every page in the wiki is currently "stale" in a structural sense**, independent of content accuracy: I recomputed SHA-256 of `wiki/orca-context-wiki.json` right now (`538fd671…`) against what `.orca/context/reviewed-startup-pack-manifest.json` has pinned as the required `wiki` source hash (`ff93b6d7…`) — **they do not match**, while the sibling `capabilities` and `graphify_catalog` pins both still match their live files. This is precisely the drift `reports/ORCA-COLLAB-STATE-AND-PRIME-AGENT-2026-08-18.md` diagnosed as the root cause of the live `ORCA_CONTEXT_NACK_V1` ("wiki content hand-edited after the manifest pinned its hash, never re-pinned"). Practical implication for this task: **adding pages to `wiki/orca-context-wiki.json` will not by itself fix the NACK, and will not "count" as reviewed/trusted content until someone runs the project's `sign_reviewed_authority.py` re-pin step with the explicit external-signing/dual-review gate the pipeline requires** (`signed_by` must not be self-signed, signature must be ≥300s old). Any expansion of the wiki should be paired with that re-pin step as a distinct, deliberately authorized action, not silently bundled into content edits.
- `orca-capability-tests` ("235 个 Orca 命令入口") — content still numerically consistent with `wiki/orca-cli-capability-inventory.json` (`commandCount: 235`), so not factually stale, but that catalog file was last generated 2026-08-11 while multiple closures since then (SSD, R2, Prime Agent, memory-bridge) plausibly touch CLI-adjacent surface; worth a fresh `orca_capability_catalog.py` regen before trusting the count going forward.
- `orchestration-lifecycle` / `resource-gate` — describe the capacity gate and Run/Task/Dispatch lifecycle but never mention `orchestration-dynamic-scheduler`, which is a newer, directly overlapping policy layer (account-binding, model routing) built on top of the same capacity gate. As written, a reader of the wiki would not know this second layer exists.
- `ego-capability-regression` — points at `scripts/verify_ego_capabilities.sh` and calls out "`dispatchKey` 为已知缺口" as a still-open gap; I did not re-verify this claim in this pass, but given how much churn has happened elsewhere since, it's a candidate for a freshness re-check rather than an assumed-stale flag.

---

### (3) Will the flat pages+links structure scale?

**No — it needs a structural rethink, not just more pages.** Reasons observed directly from the survey:

- **No grouping/namespace.** All 14 pages sit in one flat array linked ad hoc (`documents`, `governed_by`, `scoped_by`, …). Adding the ~13 candidates above (each themselves an umbrella over 4-15 sub-efforts, e.g. the SSD family alone is 4 directories, the startup-installer family is 6+) would roughly triple page count with no category boundary — the link graph would become a dense, unreadable mesh rather than a navigable map.
- **No representation of round/version history.** Several of the missing subsystems (`install_bridge.py`, `install_prime_agent.py`) have 10-17 sequential review rounds each producing a full report file. The current schema has one `status` string per page and no way to say "this is round 17 of an ongoing saga, superseding rounds 1-16" without either (a) one page per round — unworkable — or (b) collapsing to a single current-state page that silently drops the history trail the review process itself treats as load-bearing evidence.
- **No status taxonomy for "installed" vs. "reviewed candidate, never installed."** This distinction is the single most repeated fact across the whole repo (`GO_OFFLINE_CANDIDATE_ONLY` appears in nearly every closure) and is safety-critical — conflating "double-reviewed and GO" with "actually installed and running" is exactly the kind of mistake the user's global rules are designed to prevent. The current `status` field (`live-verified`, `schema-complete`, `user-approved-policy`, …) is free-text and does not encode this distinction as a structured, queryable field.
- **No link from the wiki to the project's own master index.** `reports/COMPLETION-BACKLOG-2026-08-16.md` already performs the categorization work (9 numbered domains) that the wiki lacks; the two artifacts should either be merged conceptually (wiki pages as leaves under the backlog's categories) or explicitly cross-linked, not left as two disconnected indexes of the same project.
- **The self-referential integrity problem** (finding in §2) means the wiki's growth is gated by a process (external-signed re-pin) the flat JSON schema itself gives no visibility into — there's no field today for "this page's containing file's pinned hash is currently stale."

**Recommendation shape** (not a full redesign, just the direction the evidence points): introduce a `category`/`domain` field (e.g. `startup-authority`, `memory-bridge`, `prime-agent`, `ssd-hardening`, `r2-backup`, `paperclip`, `context-bridge-pipeline`, `orchestration`) so pages can be grouped and rendered as a tree/index rather than one flat list; add a structured `deployment_state` enum (`installed-live` / `offline-candidate-reviewed` / `offline-candidate-not-reviewed` / `no-go-closed`) separate from the free-text `status`; and for multi-round sagas, one page per subsystem with a `latest_round` pointer plus a `history` array of prior round summaries, rather than either one page per round or silent history loss.

**Files touched/read during this survey** (paths for the parent agent's reference, nothing modified):
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/wiki/orca-context-wiki.json`, `.../orca-context-bridge/SKILL.md`, `.../orca-context-bridge/scripts/` (listing), `.../claude-codex-memory-bridge/README.md`, `.../prime-agent-integration/README.md`, `.../reports/COMPLETION-BACKLOG-2026-08-16.md`, `.../reports/ORCA-COLLAB-STATE-AND-PRIME-AGENT-2026-08-18.md`, `.../reports/install-bridge-install-recommendation-2026-08-20.md`, `.../reports/write-trigger-status-2026-08-19.md`, `.../reports/orca-prime-agent-gap-audit-20260815.md`, `.../r2-run-identity-closure/closure-report.md`, `.../archive-provenance-normalization-closure/REPORT.md`, `.../android-retained-retirement-closure/REPORT.md`, `.../startup-transaction-closure/README.md`, `.../orchestration-dynamic-scheduler/README.md`, `.../paperclip-orca-closure/*.md`, `.../.orca/context/reviewed-startup-pack-manifest.json`, `.../wiki/orca-cli-capability-inventory.json`.