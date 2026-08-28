# 审计 3：活跃 worktree 抽查（w2-memory-skills / w10-scheduler / w0-startup-path）

# Discovery Sweep: 3 Orca App Worktrees

Base repo: `/Volumes/Extreme SSD/Orca/projects/orca` (main = `828b5d8a96`, a single "Initial commit" — this whole multi-worktree tree is a set of divergent feature branches, none merged to main yet).

---

## 1. `orca-w2-memory-skills-9959`

**What it's for:** A standalone Python "Agent Memory Sidecar" at `tools/orca-agent-memory/` — an independent local L0-L3 tiered memory store for cooperating AI agents (unix-socket daemon, MCP adapters, semantic/hybrid retrieval, small-model/cloud extraction, Tencent-gateway A/B harnesses). It coexists with (but is architecturally separate from) the pre-existing native `orca memory` TS CLI feature (`src/cli/handlers/agent-memory.ts`) that's already on all three worktrees' shared base commit.

**Activity:** Very active — 6 commits Aug 9–13, 2026, +31,246/-76 lines across 147 files. `memory_storage.py` alone is 6,852 lines/370KB; `orca_memory.py` 1,809 lines; `memory_mcp_stdio.py` 777 lines; 12 doc files under `docs/`.

**Real state: partially working — clean code, but a genuine, reproducible test-suite failure.**
- No TODO/FIXME/XXX/HACK markers anywhere in source or tests.
- Ran the project's own documented verification command for real: `python3 -W error::ResourceWarning -m unittest discover -s tests -v` → **261 tests, FAILED (failures=5, errors=16)** — 21 real failures (~8%).
- Root-caused all 21 to one bug: `safe_parent.py::_canonical_lexical_path` unconditionally raises `SafeParentError("... has a symbolic-link parent")` if a path's parent resolves through any symlink. Python's own `tempfile` module default location on macOS (`/var/folders/.../T`) sits behind the OS's standard `/var → /private/var` symlink, so every test exercising `small_model_extraction.py`, `embedding_openai_bridge.py`, `semantic_retrieval.py`, `tencent_semantic_ab.py`, and `memory_daemon.py`'s small-model-lock path fails outright. There's no escape hatch: the code does support an "approved storage root" override (`ORCA_AGENT_MEMORY_APPROVED_STORAGE_ROOT`/`_UUID`), but that check only runs *after* `_canonical_lexical_path` already raised, so it can't rescue this.
- This isn't purely test-harness noise — `assert_safe_parent_components` is a real production fail-closed guard on model/runtime paths, so any real user whose local small model or embedding lock lives behind a symlinked directory (very common on macOS: `/tmp`, `/var`, Homebrew Cellar symlinks) would hit the same rejection.
- Not disclosed anywhere in README's "Verification and pressure test" section as a known caveat.

**Most concrete gaps, ranked by confidence:**
1. **(High)** `make test` fails 21/261 on stock macOS, single root cause as above — genuinely reproducible right now, undocumented.
2. **(Documented, still true today)** README's own "Intentional non-goals": no Windows transport until a SID-restricted Named Pipe implementation + tests exist — Windows is currently unsupported.
3. **(Documented, informational)** Everything here is explicitly candidate/offline-only — `docs/skill-paperclip-activation-report.md` shows all Skill/Paperclip activation flags `false`; nothing here is installed or wired into a live Orca instance.

---

## 2. `orca-w10-scheduler-9959`

**What it's for:** Not a generic cron scheduler — it's Orca **orchestration's managed-account worker scheduling**: binding a specific Codex account to a newly-launched orchestration worker (capacity catalog, wave planning, RPC methods, DB schema v25→v26 migration, receipts). See `docs/reference/orchestration-managed-account-scheduling.md`.

**Activity:** Lightest of the three — 1 commit (`4a58589d9e`, Aug 11) on top of shared base, but substantial: 36 files, +2083/-68 lines across CLI, main-process RPC, DB layer, shared types.

**Real state: working, cleanly, verified.**
- No genuine TODO/FIXME/XXX/HACK in the touched files (2 grep hits in unrelated pre-existing files were false positives — literal string "TODO" inside a status-migration constant name and PR-template copy, not markers).
- Ran the actual `vitest run --config config/vitest.config.ts` against the 10 test files this feature touched → **138/138 tests pass, 10/10 files pass**, ~13s.

**Most concrete gaps, ranked by confidence:**
1. **(Low/informational)** The feature doc self-scopes: "These are child-worktree implementation results, not packaged-app, external-provider, deployment, or production claims" — an honest caveat, not a bug.
2. I ran only the 10 directly-relevant test files (not Orca's full suite, which would take much longer) — solid evidence for this subsystem specifically, not a whole-app regression sweep.
3. This is the least actively-worked and most mature/clean of the three worktrees.

---

## 3. `orca-w0-startup-path-9959`

**What it's for:** A hardened "sealed" SessionStart hook-loading + crash-safe installer path for Orca's provider (Claude/Codex) context-bridge startup — schema-3-pinned, dirfd-based, hash-locked package loader (`sealed_hook_loader.py`), a 4-target installer transaction v2, and secure launch/ACK state dirs (`secure_dirs.py`). Lives at `config/scripts/orca-context-bridge-startup/`.

**Activity:** 1 commit here (`d5473828a6`, Aug 11), +4,362 lines across 15 files — **but this worktree is demonstrably stale**. The same "sealed startup context authority" line has 2 further commits merged elsewhere that this worktree lacks: `fbdabf16f3` ("feat: add sealed startup context authority candidate") is already in `orca-w0-w9-integrator-9959-v2`, `orca-w12-orchestration-mcp-9959`, and several `codex-track*` branches; a follow-on `bf14f80b1e` ("docs(orca-context-bridge): catch up SKILL.md to shipped behavior") is present in the currently-checked-out `完善orca` branch. Anyone picking up this worktree today is working from a superseded base.

**Real state: works, but only with an undocumented env var — fails out of the box.**
- The tree's own `ACCEPTANCE.md` already self-scores this: verdict `MERGE_CANDIDATE_ACCEPTED_OFFLINE`, production `NO_GO_CONTEXT_NACK` — explicitly "not installed or activated," blocked on a fresh external review pin, a live 4-target permission-completion receipt, and a real Claude/Codex trust+ACK E2E.
- Real run: `python3 -m unittest discover -s tests -p "test_*.py" -v` on the bare tree → **66 tests, 25 ERROR**, all in `test_sealed_hook_loader.py`. Its `setUp` defaults to `tempfile.gettempdir()` and calls `.chmod(0o700)` on the shared `/var/folders/.../T` parent — macOS refuses with `PermissionError: Operation not permitted`.
- Setting the (completely undocumented) `ORCA_LOADER_TEST_TMP` env var to a private directory fixes it — reran and got a clean **66/66 OK**. So the underlying loader logic is solid; this is a test-harness gap.
- No TODO/FIXME/XXX/HACK markers found anywhere.

**Most concrete gaps, ranked by confidence:**
1. **(High, current, low severity)** `tests/test_sealed_hook_loader.py` fails 25/66 (38%) out-of-the-box unless the caller happens to know `ORCA_LOADER_TEST_TMP` — referenced nowhere in `README.md` or `ACCEPTANCE.md`. Inconsistent with sibling test files in the same directory (`test_secure_dirs.py`, `test_installer_transaction_v2.py`), which avoid the identical problem cleanly via an in-tree `.test-tmp/` directory instead of the shared system temp dir. `ACCEPTANCE.md` even flagged this exact class of failure once already ("harness-environment correction, not a product-code exception") but the fix never made it into docs or into this one test file's default behavior.
2. **(Medium, structural)** This worktree's HEAD is 2 commits behind its own feature line, which has already progressed in sibling worktrees/branches (see above) — a real staleness/duplication risk if worked on as-is.
3. **(Self-documented, confirms it's genuinely incomplete)** Per `ACCEPTANCE.md`: production is blocked on a fresh externally-reviewed manifest SHA-256 pin, a live 4-target `orca-permission-completion-v2` receipt, and a real all-account trust/ACK E2E — none exist yet.
4. **(Worth flagging, not necessarily a bug)** ~15 files / ~4,300 lines of dedicated security-review tooling (with its own acceptance verdict, SHA-256 source-identity manifest, and cross-references to an unrelated sibling path in a different local workspace, `完善orca/orca-context-bridge/`) live under the Electron app's `config/scripts/`, whose only other subdirectories (`fixtures/`, `windows-apphang-repro/`) are much lighter-weight build/repro scripts — possible scope creep rather than an intended permanent home.