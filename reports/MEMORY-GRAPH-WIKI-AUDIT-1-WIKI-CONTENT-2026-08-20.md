# Audit 1: wiki 内容现状与既有过期情况

## 1. Schema of `wiki/orca-context-wiki.json`

Read in full (229 lines). Top-level keys: `version`, `project`, `pages`, `links`.

- **`pages[]` objects** — exactly 5 keys on every entry, no more, no less: `id`, `title`, `path`, `summary`, `status`. 14 page entries total.
  - `status` values in use: `partial-live-verified`, `live-verified` (×8), `user-approved-policy`, `read-only-verified`, `awaiting-test-scope`, `schema-complete`.
- **`links[]` objects** — exactly 3 keys: `from`, `to`, `relation`. 24 link entries, all `from`/`to` values resolve to existing page `id`s (checked).

**No timestamp-like field exists anywhere in the file.** I grepped for `created|updated|verified|checked|mtime|date|timestamp|last_?verified|as_?of` (case-insensitive) across the whole JSON — zero matches. Confirmed independently by enumerating every key found across all page/link objects in Python (`{'path','title','status','id','summary'}` and `{'from','to','relation'}`) — there is no 6th key on any object. Whatever "staleness" a reader wants to know has to come from the linked file's own mtime/git history or its own in-body "更新: YYYY-MM-DD" line, not from the catalog itself.

## 2. Per-page audit

All 14 referenced paths exist on disk. Git history is almost entirely absent — 13 of 14 target files have **never been committed** in this repo (`git status` shows `??`, `git log --all` on the path returns nothing); the one exception is `agent_capacity.py`, whose single commit is directly implicated in the drift below.

| id | path | exists | file mtime | last git commit | content matches summary? |
|---|---|---|---|---|---|
| orca-capability-tests | wiki/Orca能力测试与知识图谱.md | yes | 2026-08-10 18:50 | none (untracked) | Mostly — structure/policy text matches; the specific "16/18, account list + worktree current failed" evidence line is now stale (see §3) |
| multi-agent-routing | 本机Claude-Codex-Orca多Agent操作手册.md | yes | 2026-08-10 21:32 | none (untracked) | Yes, matches the "结论先行" table and routing rules verbatim |
| codex-claude-bypass-dispatch | wiki/Codex向Claude派单bypass策略.md | yes | 2026-08-15 01:35 | none (untracked) | Yes, summary is a near-verbatim compression of the doc's "强制规则" section |
| resource-gate | 本机Claude-Codex-Orca多Agent操作手册.md | yes | 2026-08-10 21:32 | none (untracked) | Matches the **document text**, but the document itself no longer matches the **live script** it cites (critical drift, §3) |
| capacity-preflight | orca-context-bridge/scripts/agent_capacity.py | yes | 2026-08-16 19:15 | 2026-08-16 19:17 `76e7d88ccb` "feat(capacity): remove gate enforcement entirely per repeated user instruction" | **No** — summary says it "输出红黄绿门槛和本轮新增 worker 上限"; the script now hardcodes `gate: "gate_removed"`, `new_workers_default: 2`, `new_workers_max: 3` regardless of host state (critical drift, §3) |
| orca-readonly-probe | orca-context-bridge/scripts/orca_readonly_probe.py | yes | 2026-08-08 21:02 | none (untracked) | Code matches (18 fixed read-only probes incl. `accounts`, `worktree_current`) but the recorded outcome is stale — I re-ran it live and got 18/18 passed, not 16/18 |
| orca-cli-capability-catalog | wiki/orca-cli-capability-inventory.json | yes | 2026-08-11 10:27 | none (untracked) | `commandCount: 235` matches; embedded `liveProbe` snapshot (17 passed, only `accounts` failed) is a **third, different** value from both the Aug-10 doc and today's live rerun (§3) |
| orchestration-lifecycle | wiki/Orca能力测试与知识图谱.md | yes | 2026-08-10 18:50 | none (untracked) | Yes, matches "受监督编排" row |
| orchestration-precondition-probe | orca-context-bridge/scripts/orca_lifecycle_precondition_probe.py | yes | 2026-08-08 20:55 | none (untracked) | Yes — code logic (skip if Run bound; otherwise expect `run_required`+`effectsApplied:false`) matches summary exactly |
| sandbox-acceptance | wiki/Orca能力测试与知识图谱.md | yes | 2026-08-10 18:50 | none (untracked) | Yes, matches "下一阶段：隔离验收清单" (still 5 open items, status `awaiting-test-scope` still accurate) |
| ego-global-policy | Ego与Cloudflare域名操作手册.md | yes | 2026-08-08 19:54 | none (untracked) | Yes, matches router/hostname-tagging sections |
| ego-profile-isolation | Ego与Cloudflare域名操作手册.md | yes | 2026-08-08 19:54 | none (untracked) | Yes, matches the cross-Space cookie-leak finding and `fresh-display`/`EGO_FRESH_PROFILE_UNAVAILABLE` behavior verbatim |
| ego-capability-regression | scripts/verify_ego_capabilities.sh | yes | 2026-08-08 19:53 | none (untracked) | Yes, `dispatchKey` test is present with `required=false`, matching "已知能力缺口" |
| desktop-mcp-realtime-control | desktop-mcp/README.md | yes | 2026-08-11 02:59 | none (untracked) | Mostly — architecture description matches, but the specific `p95 8.98ms`/60Hz benchmark numbers in the summary do **not** appear anywhere in `desktop-mcp/README.md` itself; that data actually lives only in `wiki/Orca能力测试与知识图谱.md`, a different tracked file (minor path-attribution mismatch) |

## 3. Staleness / drift found

**Critical: the capacity gate the page catalog and the user's own global CLAUDE.md rule describe no longer exists in the live script.**
- `wiki/orca-context-wiki.json` → `resource-gate` summary: *"agent_capacity.py 以 load 与 memory pressure 决定每轮 worker 数；红灯时不启动新重型 worker"*, and `capacity-preflight` summary: *"输出红黄绿门槛和本轮新增 worker 上限"*.
- `本机Claude-Codex-Orca多Agent操作手册.md` (Aug 10) still states the full red/yellow/green enforcement table and "`agent_capacity.py` 只读取状态：红灯返回 0 个新增重型 worker…" as binding.
- The user's global `~/.claude/CLAUDE.md` rule ("每一波前运行 agent_capacity.py。红灯不启动，黄灯最多一个，绿灯默认两个…") is verbatim this same policy.
- But `orca-context-bridge/scripts/agent_capacity.py`, as of commit `76e7d88ccb` (2026-08-16, "remove gate enforcement entirely per repeated user instruction"), now **always** returns `"gate": "gate_removed"`, `new_workers_default: 2`, `new_workers_max: 3` in its top-level `recommendation` field regardless of host load/memory/Orca-agent state. The true red/yellow/green computation still runs internally but is demoted to a buried `advisory_true_recommendation` field that nothing in the CLAUDE.md workflow reads. I ran the script's own docstring/code by inspection (not executed, since it queries live host state) and this is unambiguous in the source.
- **Net effect:** the wiki catalog, the handbook `.md` it links to, and the user's global instruction file all describe an enforcement behavior that was deliberately and explicitly removed six days after the handbook's last "更新" date — and the wiki has never been updated to reflect it. Anyone trusting the wiki (or the CLAUDE.md rule) believes there is still a hard red-light stop; there isn't one in this script anymore.

**Confirmed live-vs-recorded drift on the readonly-probe evidence, three different values across three sources:**
- `wiki/Orca能力测试与知识图谱.md` (Aug 10 text): "16 个成功，`account list` 与 `worktree current` 失败".
- `wiki/orca-cli-capability-inventory.json` `liveProbe` field (Aug 11 file, one day later): 17 passed, only `accounts` failed (`worktree_current` already passing).
- **Live rerun I performed just now** of the exact same `orca_readonly_probe.py`: **18/18 passed**, zero failures.
- So the "evidence" backing the `orca-capability-tests`, `orca-readonly-probe`, and `orca-cli-capability-catalog` pages is a frozen point-in-time snapshot that has already drifted twice since it was written, and neither snapshot matches current reality. Nothing in the schema would tell a reader this without independently re-running the probe or diffing file content, since (per §1) there is no timestamp field to even flag the snapshot as aged.

**Structural staleness risk (not content-specific, but systemic):** 13 of 14 linked files, plus the catalog JSON itself, are untracked in git — they have no commit history at all in this repo. `git log` cannot answer "when was this page's source last changed" for almost the entire catalog; only filesystem mtime is available, and mtime is not a reliable "last verified" proxy (a file can be touched by an unrelated copy/checkout without its content changing, or edited without anyone re-validating the claims). The one file that *is* tracked (`agent_capacity.py`) is tracked specifically because of the change that broke the wiki's claim about it — i.e., the only piece of git evidence in the whole catalog is evidence of drift, not evidence of freshness.

**Minor:** `desktop-mcp-realtime-control`'s summary cites a specific `p95 8.98ms` benchmark, but the file that page's own `path` field points to (`desktop-mcp/README.md`) contains no such number — it only describes the architecture in general terms. The benchmark figure only exists in a different page's file (`wiki/Orca能力测试与知识图谱.md`). This is a small attribution gap rather than a factual error, but it means the page's summary is not verifiable purely from its own declared source file.

**Not stale:** `sandbox-acceptance` (still correctly `awaiting-test-scope`, all 5 listed prerequisites still unmet), `orchestration-precondition-probe` (code logic matches exactly, and by design is largely time-invariant), `ego-profile-isolation`/`ego-global-policy`/`ego-capability-regression` (Ego handbook content matches its source files verbatim), and `codex-claude-bypass-dispatch` (policy doc matches the wiki summary closely).

Files consulted (absolute paths): `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/wiki/orca-context-wiki.json`, `wiki/Orca能力测试与知识图谱.md`, `本机Claude-Codex-Orca多Agent操作手册.md`, `wiki/Codex向Claude派单bypass策略.md`, `orca-context-bridge/scripts/agent_capacity.py`, `orca-context-bridge/scripts/orca_readonly_probe.py`, `wiki/orca-cli-capability-inventory.json`, `orca-context-bridge/scripts/orca_lifecycle_precondition_probe.py`, `Ego与Cloudflare域名操作手册.md`, `scripts/verify_ego_capabilities.sh`, `desktop-mcp/README.md` (all under the same repo root).