# Audit 2: 知识图谱 schema 与时间戳现状

# Report: Timestamp presence in `build_knowledge_graph.py`

Files read in full:
- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/build_knowledge_graph.py`
- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/build_context_digest.py`

## Headline answer

**The claim "`generated_at` is the only timestamp in the whole output" is FALSE.** A `grep` for `_at|timestamp|datetime|time(` in `build_knowledge_graph.py` returns exactly 5 hits, and 4 of them are timestamp-shaped fields *besides* the top-level `generated_at`:

| Line | Field | Where it lands |
|---|---|---|
| 103 | `generated_at` / `version` / `schema_version` / `schemaVersion` / `authority` | copied from each upstream payload's own top level into `sources[<name>]`, in `load_json_source` |
| 388 | `"signed_at": payload.get("authority_signed_at")` | `reviewed_knowledge` node meta, in `add_reviewed_manifest` |
| 427 | `"updated_at": entry.get("updated_at")` | `session` node meta, in `add_sessions` |
| 552 | `"updated_at": pr.get("updated_at")` | `pr` node meta, in `add_github` |
| 1087 | `datetime.now(timezone.utc).replace(microsecond=0).isoformat()` | the graph's own top-level `generated_at` |

So there are already **up to 3 node-level timestamp-shaped meta fields** (`session.updated_at`, `pr.updated_at`, `reviewed_knowledge.signed_at`) plus a **conditional per-source `sources[name].generated_at`**, none of which are validated, parsed, or normalized — they are raw pass-throughs of whatever the upstream JSON payload happened to contain (could be `None`, an int epoch, a malformed string, etc.). **No edge ever carries a timestamp** — `add_edge` only ever writes `{"from", "to", "relation"}` (line 191-198), with no exceptions anywhere in the file.

## (1) Full node/edge schema per source method, with timestamp-presence column

Top-level output shape (`run_build`, lines 1085-1091):
```
{
  "version": 2,                    # GRAPH_VERSION constant, line 20
  "generated_at": "<UTC ISO8601, microsecond=0>",
  "sources": { "<name>": {"path","sha256","size_bytes", [generated_at|version|schema_version|schemaVersion|authority]}, ... },
  "nodes": [ {"id","type","label","meta"}, ... ],
  "edges": [ {"from","to","relation"}, ... ]
}
```

### `add_sessions` (405-433)
- **Node**: `session` — id `session:{session_id}`
- meta: `provider`, `source`, `cwd` (normalized), **`updated_at` = `entry.get("updated_at")`** ← timestamp, unvalidated raw pass-through
- **Edge**: `session --cwd--> project` (only if cwd present)
- Timestamp: **present** (`updated_at`)

### `add_processes` (435-516)
- **Node**: `pane` — id `pane:{pane_id}`; meta: `cwd`, `command`. No timestamp.
- **Node**: `terminal` — id `terminal:{terminal_id}`; meta: `cwd`, `command`. No timestamp.
- **Node**: `agent_process` — id `agent_process:{pid}`; meta: `pid`, `ppid`, `elapsed` (= `process.get("etime")`, a *duration* string like elapsed CPU/wall time, not an absolute timestamp), `command`. No absolute timestamp.
- **Edges**: `pane --cwd--> project`, `terminal --cwd--> project`, `agent_process --observed_in--> project` (snapshot project)
- Timestamp: **absent** everywhere in this method (the only time-adjacent field, `elapsed`, is a duration, not a point in time)

### `add_github` (518-586)
- **Node**: `pr` — id `pr:{number}`; meta: `number`, `head`, `base`, **`updated_at` = `pr.get("updated_at")`**, `body_preview`. Timestamp **present**.
- **Node**: `wiki` (GitHub-scoped) — id `wiki:{page_key}`; meta: `page`, `preview`. No timestamp at all (GitHub wiki pages have no time field captured, even though GH's API typically exposes one).
- **Edges**: `pr --repo--> project`, `pr --mentions--> pr` (self-mention text scan), `wiki --repo--> project`, `wiki --mentions--> pr`

### `add_graphify` (302-346)
- **Node**: `code_graph` — id `code_graph:{name}`; meta: `name`, `source_root`, `graph_path`, `graph_sha256`, `source_state_sha256`, `git_head`, `node_count`, `edge_count`, `graphify_version`, `catalog_status`. No timestamp field, despite carrying two separate hashes and a git head.
- **Edge**: `code_graph --indexes--> project`
- Timestamp: **absent**

### `add_capabilities` (242-277)
- **Node**: `capability` (kind `orca-cli`) — id `capability:orca-cli:{command}`; meta: `kind`, `command`, `summary`, `verification`, `boundary`. No timestamp field.
- **Edges**: none produced directly by this method (capability→route `routed_by` edges are added later, from `add_routes`).
- Timestamp: **absent**
- (Related helper `ensure_declared_capability`, invoked from `add_routes` capability-pattern resolution, produces a second `capability` sub-kind `declared-route` with the same meta shape minus real fields — also no timestamp.)

### `add_local_wiki` (588-666)
- **Node**: `wiki` (local-scoped) — id `wiki:local:{page_key}`; meta: `page`, `scope="local"`, `summary`, `path`, `status`. No timestamp field.
- **Edges**: `wiki --documents--> project`; `wiki --<relation>--> wiki` (relation text from payload `links[].relation`, default `"references"`)
- Timestamp: **absent**

### `add_reviewed_manifest` (348-403)
- **Node**: `reviewed_knowledge` — id `reviewed_knowledge:{sha256(authority)[:20]}`; meta: `authority`, `schema_version`, `pack_sha256`, `pack_size_bytes`, **`signed_at` = `payload.get("authority_signed_at")`**, `source_bindings` (per-source `"exact"`/`"unbound"` classification), `reference_only: True`. Timestamp **present**.
- **Edges**: `reviewed_knowledge --reviews--> capability` (all), `--reviews--> wiki` (**local wiki only** — it iterates `self._local_wiki_by_key`, which GitHub wiki pages never populate), `--reviews--> code_graph` (all) — each fan-out gated on that source's binding being `"exact"`.

### `add_routes` (696-871, plus inline project-alias handling)
- **Node**: `route` — id `route:{route_key}`; meta: `route_id`, `aliases`, `summary`, `status`, `authority_state`, `reference_only: True`. No timestamp field.
- **Node**: `skill` — id `skill:{sha256(skill_name)[:20]}`; meta: `name`, `authority_state`, `verification="exact_file_hash"`, `boundary`, `path`, `sha256`. No timestamp field (file content hash is captured; file mtime is not, even though it was available at hash time — see below).
- Also touches `project` nodes via `_project_id_for_selector` / `mark_project_alias_role` (same no-timestamp `project` shape as `ensure_project`/`resolve_repo_project`).
- **Edges**: `alias --alias_of--> canonical` (project aliases), `capability --routed_by--> route`, `route --uses--> skill`, `route --knowledge--> wiki` (local wiki only), `route --code_index--> code_graph`, `route --reviewed_context--> reviewed_knowledge`, `route --targets--> project`
- Timestamp: **absent** on both new node types

### Summary table — every node type in the graph

| Node type | Timestamp field(s) in meta | Source method |
|---|---|---|
| `session` | `updated_at` (raw pass-through) | `add_sessions` |
| `pr` | `updated_at` (raw pass-through) | `add_github` |
| `reviewed_knowledge` | `signed_at` ← payload's `authority_signed_at` (raw pass-through) | `add_reviewed_manifest` |
| `wiki` (GitHub) | none | `add_github` |
| `wiki` (local) | none | `add_local_wiki` |
| `project` | none | `ensure_project`/`resolve_repo_project`/`_project_id_for_selector` |
| `pane` | none | `add_processes` |
| `terminal` | none | `add_processes` |
| `agent_process` | none (only `elapsed`, a duration) | `add_processes` |
| `capability` | none | `add_capabilities` / `ensure_declared_capability` |
| `code_graph` | none | `add_graphify` |
| `route` | none | `add_routes` |
| `skill` | none | `add_routes` |

**Edges: no relation type, from any method, ever carries a timestamp field.**

## (2) Existing hashing/provenance conventions worth reusing

From `build_knowledge_graph.py` itself (the file that actually owns hashing — `build_context_digest.py` has no `hashlib` import at all and does zero content hashing):

- **`load_regular_source`** (39-86): opens every source file via `os.open(..., O_NOFOLLOW|O_CLOEXEC)`, rejects non-regular files and anything over `MAX_INPUT_BYTES` (64 MiB), reads the bytes, then re-`fstat`s the descriptor and lexically re-`stat`s the path (`follow_symlinks=False`) to build a `(dev, ino, size, mtime_ns, ctime_ns)` identity tuple and requires it to be unchanged before/after the read (TOCTOU guard). It returns `{"path": <absolute path>, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}` — this is the canonical "prove the graph was built from exactly these bytes" pattern.
- **`load_json_source`** (89-107) layers on top: parses the *same hashed bytes* as JSON, then lifts a small allowlist of top-level scalar keys (`generated_at`, `version`, `schema_version`, `schemaVersion`, `authority`) straight out of the payload into the source's provenance record, with a strict type guard (`isinstance(value, (str, int)) and not isinstance(value, bool)`). This is a directly reusable pattern for "surface a payload's own declared timestamp without inventing a new one out-of-band."
- **Two provenance-binding strengths already coexist** and a new timestamp design should pick one deliberately:
  - **Hard-fail** — `load_skill_reference_sources`/`add_routes` skill verification: an expected `sha256` pinned in the routes catalog must exactly equal the hash freshly computed by `load_regular_source`, or the whole build raises `ValueError`.
  - **Soft-degrade** — `add_reviewed_manifest`'s `shared_source_sha256s`: compared against the actually-recorded `sources[...].sha256`, classified per-source as `"exact"` vs `"unbound"`; a mismatch doesn't fail the build, it just withholds the `reviews` edges for that source.
- **`redact_text(text, home, limit)`** (imported from `build_context_digest.py`) is applied to every free-text field placed in node meta (titles, previews, summaries, commands) — strips secrets/PII via multiple regexes, collapses the home dir to `~`, truncates with a `"… [truncated]"` marker. A scalar timestamp field wouldn't need this specific function, but should follow its spirit: never store an upstream value un-typechecked (the file already does this for e.g. `pid: isinstance(pid, int) and not isinstance(pid, bool) and pid > 0` in `add_processes`, or the `re.fullmatch(r"[0-9a-f]{64}", expected)` shape check for hashes).
- **`write_private(path, content)`** (imported, defined at 1297-1389 in `build_context_digest.py`) is the single reused output sink for both scripts — atomic temp-file-then-`os.replace`, mode 0600, full symlink/TOCTOU protection, identity re-verification. Already shared via the exact import in question (`from build_context_digest import redact_text, write_private`, line 17).
- **`GRAPH_VERSION = 2`** (line 20) is a flat integer schema marker for `build_knowledge_graph.py`'s own output shape, independent of any `version`/`schema_version` scalar it might copy from an upstream source payload's own top level. Any new timestamp field that changes the emitted shape should bump this the same way prior schema changes presumably did.
- **Timestamp *formatting* convention, duplicated (not shared) between the two files**: both independently write `datetime.now(timezone.utc).replace(microsecond=0).isoformat()` for their one self-generated "when was this built" timestamp — `build_context_digest.py` line 710 (`generated`) and `build_knowledge_graph.py` line 1087 (`generated_at`). This literal expression, not a shared helper, is the de facto house style for a freshly-minted timestamp in this codebase (UTC, second precision, stdlib `isoformat()` — i.e. `+00:00` suffix, not `Z`).
- **`parse_timestamp`** in `build_context_digest.py` (139-155) is the codebase's only real timestamp *normalization* routine: accepts epoch int/float (auto-detecting ms vs s via a `>10_000_000_000` heuristic) or an ISO8601 string (`Z`→`+00:00`, `datetime.fromisoformat`, assume UTC if naive), and always returns a UTC-aware `datetime`. `Session.updated_at` is computed as `max()` of every parsed record timestamp found, falling back to the file's own `st_mtime` if none parse — this "max of parsed timestamps, else filesystem mtime" is the existing convention for "when was this thing last active" and is a strong candidate to reuse/mirror if `build_knowledge_graph.py`'s currently-unparsed `updated_at`/`signed_at` fields are ever tightened up.

## (3) Other design notes relevant to adding timestamps

- **The 3 timestamp fields that already exist are completely unvalidated.** `entry.get("updated_at")`, `pr.get("updated_at")`, and `payload.get("authority_signed_at")` are stored verbatim with no type check, no `parse_timestamp`-style normalization, and no timezone handling — in contrast to `build_context_digest.py`, which never emits a timestamp without routing it through `parse_timestamp` first. This is an existing inconsistency inside the pipeline, not just a gap: if timestamps become a first-class concern, these three fields need the same discipline the digest script already has, not a new convention invented from scratch.
- **A near-miss worth noting**: `load_regular_source` already computes `st_mtime_ns`/`st_ctime_ns` for every source file (lines 65-66) purely for its TOCTOU identity check, then discards them — they never reach the `record` dict, so not even *whole-file* freshness (e.g., "when was `capabilities.json` last written") is currently visible anywhere in the output, only whatever timestamp a payload chose to self-declare at its own top level (via the `generated_at`/`version`/etc. sniff).
- **`build_knowledge_graph.py` is a pure aggregator** — it has no independent way to know "when did this PR/session/pane/route actually happen" beyond (a) what the upstream JSON payload already declares, or (b) the moment the graph script itself runs (`generated_at`). Any new per-node timestamp for the currently-blank node types (`project`, `pane`, `terminal`, `agent_process`, `capability`, `code_graph`, `wiki` [both scopes], `route`, `skill`) has to be produced upstream (in whatever script writes `--sessions`/`--processes`/`--github`/`--wiki`/`--capabilities`/`--graphify`/`--routes`) and then threaded through here the same way `session.updated_at`/`pr.updated_at` already are — this file does not stat live processes or query GitHub itself.
- **Seven of the thirteen node types are the ones with zero timestamp exposure today**: `project`, `pane`, `terminal`, `agent_process`, `capability`, `code_graph`, `wiki` (both `wiki:` and `wiki:local:` scopes), `route`, `skill`. Some of these plausibly have no natural "event time" (`capability` describes a static reviewed command catalog entry; `route`/`skill` are reference-only catalog entries whose only meaningful time is the hash-pinned file's own mtime, which is presently discarded per the point above). Others clearly do have a natural analogue that's simply not being read: `pane`/`terminal` could carry a process/session start time if the upstream `--processes` JSON captured one (it isn't consulted at all beyond `cwd`/`command`); `agent_process` already captures a duration (`etime`) but not the absolute start time it would be derived from.
- **`add_reviewed_manifest`'s `reviews` edge to `wiki` only ever reaches local-scoped wiki nodes** (`self._local_wiki_by_key`), never GitHub-scoped wiki nodes — relevant if a future timestamp-provenance design wants review/staleness edges to cover *all* wiki nodes uniformly, since the existing binding mechanism doesn't currently do that even for hashes/reviews, let alone timestamps.