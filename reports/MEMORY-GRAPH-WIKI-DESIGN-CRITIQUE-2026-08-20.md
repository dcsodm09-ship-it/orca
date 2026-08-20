# 对抗性复核意见

# Adversarial Review: Wiki/Knowledge-Graph Timestamp Design

I read `wiki/orca-context-wiki.json` (228 lines, 14 pages/link set), `orca-context-bridge/scripts/build_knowledge_graph.py` (1123 lines), `orca-context-bridge/scripts/build_startup_bundle.py`'s `verify_reviewed_pack`, the live `.orca/context/reviewed-startup-pack-manifest.json`, and `orca-context-bridge/reviewed-pack-multi-project-fix/sign_reviewed_authority.py`. The design is well-organized and its intent (four distinct timestamp semantics, no TTL, honest `null` over fabricated `verified_at`) is sound, but several of its concrete diffs do not match the actual code, and the mechanism that's supposed to close the Audit 3 gap is not actually delivered by what's specified as implementable. Findings below, most severe first.

## 1. (Critical) The concretely-specified schema change silently breaks an existing consumer: `build_knowledge_graph.py`'s `add_reviewed_manifest` binding computation

`orca-context-bridge/scripts/build_knowledge_graph.py:375-378`:
```python
for manifest_key, graph_key in source_map.items():
    expected = source_hashes.get(manifest_key)
    observed = sources.get(graph_key, {}).get("sha256")
    bindings[manifest_key] = "exact" if expected and expected == observed else "unbound"
```
Section 2.2.3's proposed manifest change turns `shared_source_sha256s.wiki` from a plain sha256 string into `{"sha256":..., "content_version":..., "pinned_at":...}`. This line does `expected == observed` where `observed` is always a bare hex string — a dict will never equal it, so `bindings["wiki"]` silently and permanently flips to `"unbound"` even when the wiki is genuinely bound and fresh. This is exactly the kind of "existing consumer of the file" break the task asked me to check for, and it is not mentioned anywhere in the design (Section 5.2's diff for `add_reviewed_manifest` only touches the `signed_at`→`timestamps` line, not this one).
**Fix**: Section 5.2's diff must also touch this loop — e.g. `expected_hash = expected.get("sha256") if isinstance(expected, dict) else expected`.

## 2. (Critical) `verify_source_binding` (§2.2.4) is not wired to the real `verify_reviewed_pack` and would not run as literally proposed

The real per-source loop is `build_startup_bundle.py:1416-1447`, not a standalone `verify_source_binding(name, path, expected)` function. Two concrete problems:
- Line 1443: `if expected is not None and not valid_sha256(expected): raise ValueError(f"...source hash invalid: {name}")`. `valid_sha256` (line 890) does `isinstance(value, str)` first — a dict fails this immediately. So with the proposed dict-shaped `wiki` entry, **every session start would fail with "source hash invalid: wiki"** before the design's new isinstance-dispatch logic is ever reached, regardless of whether content actually drifted. The design's own §2.2.4 sample code is never actually called by the real file.
- The real loop also carries `shared_source_policy` (`required`/`reason`) and accumulates `observed_sources` per name across all of `capabilities`/`wiki`/`graphify_catalog`(/`routes`) — none of that is threaded through the sketch. Someone implementing "directly from this diff" would have to substantially rewrite lines 1416-1447, not drop in a new function.
**Fix**: Provide the actual diff against `build_startup_bundle.py:1416-1447`, including moving the `valid_sha256` check inside the `isinstance(expected, str)` branch.

## 3. (Critical) The live manifest is schema_version 2, not 3/4 — the design's manifest example is already stale before its own changes are applied

`.orca/context/reviewed-startup-pack-manifest.json` on disk right now has `"schema_version": 2` and top-level keys `authority_git`, `authority_signed_at`, `signed_by`, `signature_nonce` — none of which are in the *current* `verify_reviewed_pack`'s accepted key set (`{"schema_version","authority","pack","shared_source_sha256s","shared_source_policy","project_git","content_source_closure"}`, line 1357) or in `REVIEWED_PACK_MANIFEST_SCHEMA_VERSIONS = {3,4}` (line 80-82). This manifest would already fail `verify_reviewed_pack` today, independent of this design. Meanwhile the design's §2.2.3 example manifest and its repeated invocation of `sign_reviewed_authority.py`'s "non-self-sign, ≥300s aging" constraint describe the *old* `signed_by`/`authority_signed_at`/`signature_nonce` v2 concept — but the current v3/v4 validation code has already dropped signature fields entirely in favor of `content_source_closure` + git identity, and the only `sign_reviewed_authority.py` in the tree (`orca-context-bridge/reviewed-pack-multi-project-fix/sign_reviewed_authority.py`) is explicitly a stub ("*These would be imported from build_startup_bundle in a real scenario... we'll assume they're available or define minimal implementations*", "*This tool is NOT called automatically*"). The design treats a prototype/stub as an established, must-be-preserved production invariant, and its own illustrative manifest mixes v2 fields into what should be a v3/v4-shaped example.
**Fix**: Before specifying the manifest diff, reconcile which schema generation is actually authoritative today (v2 on disk vs. v3/v4 in code), and drop the `authority_signed_at`/`signed_by` framing unless it's confirmed to still be load-bearing somewhere outside this repo.

## 4. (High) `make_timestamps`'s own sample code has a live bug: wrong exception type

§5.2:
```python
try:
    normalized_content_at = parse_timestamp(content_at).isoformat()
except (ValueError, TypeError):
    normalized_content_at = None
```
`parse_timestamp` (`build_context_digest.py:139-155`) **never raises** — on unparsable input it returns `None` and swallows its own `ValueError` internally. So `parse_timestamp(content_at)` returning `None` followed by `.isoformat()` raises `AttributeError: 'NoneType' object has no attribute 'isoformat'`, which is not in the caught tuple. Any malformed `session.updated_at`/`pr.updated_at`/`authority_signed_at` (exactly the "unvalidated passthrough" data Audit 2 flagged as the problem) would crash the whole graph build instead of degrading to `null` as intended.
**Fix**: `parsed = parse_timestamp(content_at); normalized_content_at = parsed.isoformat() if parsed else None` — no try/except needed at all.

## 5. (High) The diffs assume state (`self.generated_at`, `sessions_source_record`, `github_source_record`, `manifest_source_record`) that doesn't exist at those call sites

`GraphBuilder.__init__` (lines 174-184) sets no `self.generated_at`; the `generated_at` value is only computed as a local variable in the top-level function at line 1087, *after* `build_graph()` (and thus all `add_node` calls) has already returned. Every timestamped node-creation snippet in §5.2 references `self.generated_at`, which will raise `AttributeError` as written. Similarly, `add_sessions(self, payload)` and `add_github(self, payload)` (lines 405, 518) currently take only `payload` — no source record — unlike `add_reviewed_manifest(self, payload, sources)` which already does. The diffs reference `sessions_source_record`/`github_source_record` as if already in scope inside those methods; they aren't, and the signatures need to change to accept `sources` too.
**Fix**: Compute `generated_at` before constructing `GraphBuilder` and pass it into `__init__`; change `add_sessions`/`add_github` signatures to accept `sources: dict[str, dict[str, Any]]` like `add_reviewed_manifest` already does.

## 6. (High) As scoped, this design does not actually close the Audit 3 incident — the answer to the reviewer's central question is "no, not yet"

The two pieces that would genuinely shrink the "10-hour gap" — `wiki_edit_guard.py` (§2.2.2) and `check_wiki_freshness.py` (§2.2.5) — are both explicitly marked "设计层描述，不在本次实现" / "设计层要求存在" (design-level only, not implemented this round). What *is* concretely specified and implementable (§5's schema diff, §2.2.4's verify change) only adds a `content_version` field checked **at session start** — the same trigger point Audit 3 already found happens ~10 hours late. Critically, `content_version` is informationally redundant with the existing sha256 check *at verify time*: if wiki content changed without repinning, the sha256 already mismatches and already fails closed today — that's the exact mechanism Audit 3 called "correct." `content_version` only adds new value at *edit time*, via the guard script that isn't being built. So the part of this design that's ready to implement changes nothing about when the drift is discovered; it only adds a second, currently-redundant check to the same existing choke point.
**Fix**: Either fold `wiki_edit_guard.py` into this design's implementable scope (it's the only piece that actually addresses the incident), or be explicit in the document that this round only adds detection *metadata*, and the incident itself remains open pending a follow-up round.

## 7. (Medium) The document promises Section 6 covers a risk that Section 6 doesn't contain

§5.2, on bumping `GRAPH_VERSION` 2→3: "*任何依赖固定 v2 形状的下游消费者需要显式适配——这本身也是第 6 节要专门提示的风险点*" (this is a risk Section 6 specifically flags). Section 6 lists exactly three risks: (1) `verify_reviewed_pack` fail-open, (2) `verified_at` fabrication, (3) mixing schema+content PRs. None of them is about `GRAPH_VERSION` consumer breakage. This is a self-referential inconsistency inside the document itself — a promised cross-reference that isn't kept.
**Fix**: Add a fourth Section 6 risk item for `GRAPH_VERSION` bump / node shape change breaking any out-of-repo consumer of the published graph JSON, or remove the false cross-reference.

## 8. (Medium) Migration's `created_at = migration execution time` for all 13 untracked pages manufactures a different, subtler false signal than the one the design guards against

§3.1/3.2 correctly refuses to fabricate `verified_at`, and that reasoning is sound. But it is less careful about `created_at`: stamping all 13 untracked pages with the *same* migration-run timestamp will make a reader see 13 pages that all "look equally new," even though these entries were plainly added to `wiki/orca-context-wiki.json` at different points over the project's history (the file already existed and was pinned back on 2026-08-14 per the live manifest; some entries are demonstrably older content than others per Audit 1). The design's own definition ("目录化时间，不是源文件创建时间") is defensible on a strict reading, but nothing warns a downstream reader/UI that a uniform migration-day `created_at` carries no relative-staleness information — which is the same class of misleading-signal risk the design is otherwise vigilant about (cf. its Risk 2 treatment of `verified_at`).
**Fix**: Either record something that preserves relative ordering (e.g., array index order at migration time as a tiebreaker note) or add an explicit caveat next to `created_at`'s definition: "a shared migration-day value across legacy pages carries no relative-recency information."

## 9. (Minor) `add_node`'s existing signature uses different parameter names than the diff shows

Actual: `def add_node(self, node_id: str, node_type: str, label: str, meta: dict[str, Any]) -> None` (line 186). The §5.2 diff shows the "before" state as `def add_node(self, id_, type_, label, meta):` — cosmetic, but it means the diff wasn't generated against the actual file, which is a small additional signal (alongside findings 1, 2, 5) that the "concrete enough to implement directly" claim in §5.2 overstates fidelity to the real code.

## 10. (Minor) No accessible Audit 1–4 documents in the repo to verify citations against

The design repeatedly cites "Audit 1 §2/§3", "Audit 4 §2" as justification for specific page priorities and content corrections. I could not find any committed audit report matching that description anywhere in the repo (searched by filename and via `git log --all --oneline | grep -i audit`). I *did* independently verify that every directory/file the 14 new-page proposals reference genuinely exists (`ssd-native-storage-closure`, `r2-run-identity-closure`, `paperclip-orca-closure`, `orchestration-dynamic-scheduler/scheduler.py`, `skills/review-orca-workflow-learning/scripts/skilld.py`, `reports/COMPLETION-BACKLOG-2026-08-16.md`, `orca-context-bridge/scripts/context_route.py`, etc. — all present), so the priority list is not arbitrary or fabricated in the sense of pointing at things that don't exist. But since the source audits aren't in the repo, the specific *priority ordering* (why P0 vs P1 vs P2 for these particular 14) is not independently checkable — it rests on unpersisted claims.
**Fix**: Commit the Audit 1-4 reports (or fold their load-bearing findings inline into this design doc) so the priority ordering is self-contained and auditable, consistent with this project's own dual-review/traceability norms.

## What holds up

- The wiki file genuinely has 14 pages, 13/14 untracked in git — the migration table's premises (§3.1/§1.3) are accurate, verified directly against `git log --follow` counts.
- `add_local_wiki`/`add_github`/link processing in `build_knowledge_graph.py` do not do strict-field-set validation on wiki pages or links (unlike `add_routes`'s `skill_references`), so additive fields (`created_at`/`updated_at`/`verified_at`/`verification_status`/`source_mtime`) are genuinely additive/non-breaking for that specific ingestion path — the schema-compatibility claim is correct for §5.4's wiki-JSON-side diff, just not for the manifest/graph-node side (see findings 1-3).
- `parse_timestamp` really does accept both `Z` and `+00:00` and normalizes to UTC — the "read tolerant, write canonical" policy is grounded in real code.
- The `verified_at = min(from, to)` derivation rule for links and the "no TTL, no edge timestamps (derive from node)" decisions are internally coherent and well-argued, not just asserted.
- All 14 wiki page `path` values currently resolve to real files, so the "source_mtime backfill risk = zero" claim holds for the present snapshot (though the migration spec should still define a fallback for a future missing-file case).