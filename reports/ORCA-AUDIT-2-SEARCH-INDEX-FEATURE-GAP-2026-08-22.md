# 审计 2：Orca CLI 搜索/索引能力实测

## Findings: Code-search / indexing / symbol-navigation in the real Orca app

**Investigation basis:** `orca --help`, `orca agent-context --json` (full 231-command machine-readable schema), `orca skills get orca-cli`, live exercise of every search-shaped CLI command against the live runtime (app pid 1696, runtime `f7b09db6…`), plus source reading across `orca-w2-memory-skills-9959` (richest hit set) cross-checked against `orca-w0-startup-path-9959`, `orca-w3-r2-migration-9959`, `orca-w6-native-release-9959`, `orca-w10-scheduler-9959`, `orca-w12-orchestration-mcp-9959`, `orca-history-multiroot` (all currently at the same base commit for these files).

Note on repo layout: `/Volumes/Extreme SSD/Orca/projects/orca` (the "main" checkout `orca --help` etc. actually runs against) has **zero tracked files** on branch `main` (`git ls-tree -r --name-only HEAD` → 0 lines) — real source only exists in the feature-branch worktrees, which is why the search had to happen there.

### 1. What exists and genuinely works (verified live)

- **`orca repo search-refs`** — real, works. Searches git branch/tag *ref names* only, not file content.
  - Verified: `orca repo search-refs --repo id:35e82c5e-5eed-4052-b898-b0dff70ade97 --query index --limit 10 --json` → returned 10 branch names like `origin/fix-index-head-refresh`, `truncated: true`. Empty query correctly returns `refs: []`.
- **`orca linear search`** — real, works, but it's *Linear ticket* search, not code search. Not listed in top-level `orca --help` output at all (only surfaces via `orca linear --help` or `agent-context --json`), so the top-level help is not a complete command index.
  - Verified: `orca linear search "test" --json` → `{"ok":false,"error":{"code":"linear_not_connected", ...}}` — correct graceful failure since Linear isn't connected here.
- **`orca file open` / `file diff` / `file open-changed`** — real, works, but is strictly "open this exact path in the Orca GUI editor," not a read/content/search operation.
  - Verified: `orca file open --path .gitignore --json` → `{"opened":true,"kind":"text"}` (really opened a tab in the live app).
  - Verified: `orca file open --path this-file-does-not-exist-xyz.txt --json` → exit 1, `ENOENT: no such file or directory` — no fuzzy/quick-open matching, exact path required.
- **In-app "Search" sidebar (GUI only, not CLI-reachable)** — a real, mature, well-tested VS-Code-style workspace text search:
  - `main/ipc/filesystem-search-git.ts` — git-grep fallback engine.
  - `main/ipc/rg-availability.ts` — `rg --version` probe (5s timeout) to decide rg vs git-grep.
  - `renderer/src/components/right-sidebar/{SearchResultsPane,SearchFilters,SearchQueryRow,useFileSearchPanel,useFileSearchRunner}.ts(x)` — full UI: case-sensitive/whole-word/regex toggles, include/exclude glob patterns (`file-search-include-pattern.ts`), virtualized result rows (`search-rows.ts`).
  - `shared/types.ts:3780-3809` — `SearchMatch`/`SearchFileResult`/`SearchResult`/`SearchOptions` types.
- **"Quick Open" (Cmd+P-style filename search, GUI only)** — backed by ripgrep with install guidance when missing: `shared/quick-open-install-rg.ts`, `renderer/src/components/quick-open-install-rg-guidance.tsx`, `main/ipc/filesystem-list-files*.ts`.

### 2. Broken / incomplete / undocumented

1. **The app's own text-search engine is never wired to the CLI.** `filesystem-search-git.ts` / `rg-availability.ts` / the whole `SearchResult` type family exist and are exercised only via renderer IPC. Cross-checked against the *complete* 231-command schema (`orca agent-context --json`) — grepping every command's JSON for `search|index|symbol|ripgrep|ctags|grep` (case-insensitive) returns only: `computer *` (unrelated accessibility flags), `linear search` (Linear tickets), `repo search-refs` (git refs), `tab switch/close` (`--index` flag, unrelated). **Zero CLI commands surface file-content search.** An agent driving Orca headlessly through `orca` has no way to grep repo content through Orca itself — it must fall back to raw shell tools, defeating the purpose of a CLI meant for "agent discovery" (`orca agent-context`).
2. **`orca --help`'s top-level listing is not a complete command index.** `linear search` exists and works but isn't shown under the top-level "Linear:" section (only "linear — Read Linear ticket context for agents" is listed); it only appears via `orca linear --help` or the JSON schema. Any other subcommands hidden the same way would be effectively undiscoverable without reading the JSON schema or source.
3. **`file open --path` has no fuzzy/glob matching**, unlike the GUI's Quick Open. `cli/specs/file.ts:6-18` documents only exact relative/absolute paths; confirmed live by the ENOENT test above. There's no `file` subcommand to list a worktree's files or read content at all (`cli/specs/file.ts` defines exactly 3 commands: `open`, `diff`, `open-changed` — none read/list/grep).
4. **Silent-ish truncation with no pagination.** `useFileSearchRunner.ts:17` caps at `SEARCH_MAX_RESULTS = 2000`; `shared/text-search.ts:57` caps at `SEARCH_TIMEOUT_MS = 15_000`. On truncation the only surfaced feedback is a small `"(results truncated)"` string in `SearchResultsPane.tsx:76-77` — no way (CLI or otherwise) to page through the remainder of a large repo's matches.

### 3. Conspicuously absent capabilities (confirmed, not assumed)

- **No symbol index / LSP integration whatsoever.** Directly acknowledged in source:
  `renderer/src/components/editor/monaco-codebase-search.ts:33-35`
  ```
  // Why: until Orca has semantic LSP references, the editor affordance should
  // still work from a cursor by searching the visible symbol text in files.
  return normalizeSelectedTextForFileSearch(model.getWordAtPosition(position)?.word)
  ```
  i.e. "go to definition"/"find references"/"workspace symbols" don't exist — the editor's "codebase search" affordance just does a plain-text search of the word under the cursor. Confirmed by targeted grep for `language.?server|lsp|go.?to.?definition|documentSymbol|workspaceSymbol|find.?references` across all of `src/` returning zero real hits (all matches were unrelated identifiers like `linear.ts`, `speech/model-catalog.ts`, terminal/pty code).
- **No ctags or any symbol database anywhere** — grep for `ctags` (case-insensitive) across `src/` in every checked worktree returns zero matches.
- **No persistent/background code index.** Grep for `fts|sqlite.*index|inverted.?index|trigram|bleve|tantivy|elasticsearch|lunr|flexsearch` returns only unrelated hits in the orchestration task database (`main/runtime/orchestration/db.test.ts`, `orchestration-schema-version-skew.ts`) — nothing for code/file content. Every search is a fresh live `rg`/`git grep` subprocess spawn (`main/ipc/filesystem-search-git.ts`, `rg-availability.ts`), scoped to whatever's on disk right now — no warm index, no incremental updates, no offline/background indexing step at startup.
- **No cross-worktree search.** `SearchOptions.rootPath` (`shared/types.ts:3804`) is a single string, and `useFileSearchRunner.ts:63,94-125` binds every search call to exactly one `activeWorktreeId`/`worktreePath` — the currently focused worktree tab. With 38 worktrees registered on this machine (`git worktree list`), there is no aggregate/global search UI or API across them; a user (or agent) must switch to each worktree individually and re-run the search per-tab. Nothing in the CLI schema (`file`, `repo`, `worktree` command groups) offers a multi-worktree search either.

### Summary
Orca ships one real, working, well-tested code-search feature (VS-Code-style ripgrep/git-grep text search + Quick Open filename search), but it is **entirely GUI/IPC-only** — the public `orca` CLI (231 documented commands) exposes none of it: no content grep, no file listing/reading, no fuzzy file-open, and the one CLI "search" verb that does exist (`repo search-refs`) only searches git ref names. There is no symbol index, no LSP/go-to-definition/find-references (explicitly deferred per source comment), no persistent index engine, and no cross-worktree search — every search is a single-worktree, on-demand subprocess spawn capped at 2000 matches / 15s with no pagination.