import type Database from '../../sqlite/sync-database'

// Why (round 9, fix for a systemic issue an independent review surfaced then a full sweep of
// every entry against migrate-v2-v12.ts/migrate-v13-v28.ts/migrate-legacy-contract-storage.ts
// confirmed): this list must hold ONLY columns present as of the TRUE v7 baseline (the first
// migration past the original v6 schema) - anything added by a LATER migration belongs in
// VERSIONED_POST_V6_COLUMNS below instead, or a healthy database at any version between v7 and
// that column's real introduction gets wrongly judged "incomplete" and rewound to 6. Verified:
// these 4 `run_id` columns are the ONLY entries that actually land at v7 (`if (current < 7)`'s
// ALTER loop in migrate-v2-v12.ts); every other entry that used to be here turned out to be a
// later addition and has been moved down.
const POST_V6_COLUMNS = [
  ['messages', 'run_id'],
  ['tasks', 'run_id'],
  ['dispatch_contexts', 'run_id'],
  ['decision_gates', 'run_id']
] as const

const VERSIONED_POST_V6_COLUMNS = [
  // Why: migrate-task-terminal-states.ts's INSERT-SELECT explicitly names these three columns -
  // without probing for them here, a DB missing only this later v13-v28 addition would still be
  // judged "post-v6, skip re-migrating" and then crash the whole OrchestrationDb constructor
  // (not just createTask()) the first time that later migration runs against it.
  // Why versioned, not unversioned (round 7, fix for a real regression review found): these
  // three land at v24 (migrate-v13-v28.ts's `if (current < 24)` block) - putting them in the
  // unversioned list instead made a perfectly healthy v7-v23 database (one that predates v24 and
  // has never needed these columns) look "incomplete" and rewound its migration start version
  // all the way back to 6, re-running every migration from v7 - including several live-data
  // backfills - unnecessarily.
  // Why the rest of this list (round 9): the SAME regression class, found pre-existing in every
  // other originally-unversioned column - each entry below is version-checked against its real
  // introducing migration, not assumed to be a v7 baseline column. `question_threads` itself is
  // created at v8 (migrate-v2-v12.ts); the dispatch_contexts capability trio at v10; worker_
  // dispatches.runtime_epoch at v13; the federation/relay columns and table at v15;
  // remote_questions at v16; remote_dispatch_attachments.protocol_version at v17; the legacy-
  // contract-storage columns and tables (messages.delivery_contract, coordinator_runs.
  // scheduler_lost_at, dispatch_contexts.contract_version/launch_token_hash, and all 4
  // legacy_* tables) at v19 (migrate-legacy-contract-storage.ts, called from `if (current < 19)`).
  { version: 8, table: 'question_threads', column: 'run_id' },
  { version: 10, table: 'dispatch_contexts', column: 'capability_hash' },
  { version: 10, table: 'dispatch_contexts', column: 'process_incarnation' },
  { version: 10, table: 'dispatch_contexts', column: 'capability_revoked_at' },
  { version: 13, table: 'worker_dispatches', column: 'runtime_epoch' },
  { version: 15, table: 'federated_dispatches', column: 'to_home_imported_sequence' },
  { version: 15, table: 'remote_dispatch_attachments', column: 'to_worker_imported_sequence' },
  { version: 15, table: 'federation_relay_items', column: 'dispatch_id' },
  { version: 16, table: 'remote_questions', column: 'message_id' },
  { version: 17, table: 'remote_dispatch_attachments', column: 'protocol_version' },
  { version: 19, table: 'messages', column: 'delivery_contract' },
  { version: 19, table: 'coordinator_runs', column: 'scheduler_lost_at' },
  { version: 19, table: 'dispatch_contexts', column: 'contract_version' },
  { version: 19, table: 'dispatch_contexts', column: 'launch_token_hash' },
  { version: 19, table: 'legacy_adoptions', column: 'source_run_id' },
  { version: 19, table: 'legacy_compatibility_principals', column: 'id' },
  { version: 19, table: 'legacy_operation_receipts', column: 'principal_id' },
  { version: 19, table: 'legacy_mail_receipts', column: 'principal_id' },
  { version: 24, table: 'tasks', column: 'created_by_pane_key' },
  { version: 24, table: 'tasks', column: 'created_by_process_incarnation' },
  { version: 24, table: 'tasks', column: 'created_by_run_generation' },
  { version: 27, table: 'federated_dispatches', column: 'to_home_acknowledged_sequence' },
  // Why (round 11, fix for the SAME regression class a genuinely re-verified independent review
  // found beyond what round 9's "complete" sweep covered): round 9 only grepped
  // migrate-v2-v12.ts/migrate-v13-v28.ts/migrate-legacy-contract-storage.ts and stopped at v27 -
  // it never checked migrate.ts itself (which has its own inline v29-v31 blocks, not delegated to
  // migrate-v13-v28.ts despite that file's name) nor v11's/v28's table-creation blocks. mutation_
  // receipts is created at v11 (migrate-v2-v12.ts); mutation_caller_identities at v28
  // (migrate-v13-v28.ts); tasks.terminal_reason/replacement_task_id at v29
  // (migrate-task-terminal-states.ts, called from migrate.ts's `if (current < 29)`);
  // worker_dispatches.terminated_by at v30 and dispatch_contexts.stale_escalated_at at v31 (both
  // inline in migrate.ts).
  // Why the v11/v28 entries below (and v22's index further down in VERSIONED_POST_V6_INDEXES)
  // can never independently PROVE a false rewind in production (round 12 accuracy note, not a
  // functional issue - flagged by review, kept anyway): createTables() runs its full current-
  // schema CREATE ... IF NOT EXISTS pass before migrate() ever does, so mutation_receipts/
  // mutation_caller_identities/idx_dispatch_assignee_handle are always healed before the
  // resolver looks. Keep them anyway - they're still correct, still cheap, and document real
  // facts about when each artifact was introduced; do not read their presence here as "this is
  // the only thing standing between a healthy old database and a false rewind" for these three.
  { version: 11, table: 'mutation_receipts', column: 'state' },
  { version: 26, table: 'mutation_receipt_ledger', column: 'singleton' },
  { version: 28, table: 'mutation_caller_identities', column: 'transport' },
  { version: 29, table: 'tasks', column: 'terminal_reason' },
  { version: 29, table: 'tasks', column: 'replacement_task_id' },
  { version: 30, table: 'worker_dispatches', column: 'terminated_by' },
  { version: 31, table: 'dispatch_contexts', column: 'stale_escalated_at' }
] as const

// Why (round 10, fix for the SAME regression class an independent review's follow-up audit
// confirmed): only these 5 are genuine v7 baseline indexes (migrate-v2-v12.ts's
// `if (current < 7)` block) - the other 6 that used to be listed here are later additions and
// have been moved to VERSIONED_POST_V6_INDEXES below.
const POST_V6_INDEXES = [
  'idx_messages_run_sequence',
  'idx_tasks_run_status',
  'idx_dispatch_run_status',
  'idx_gates_run_status',
  'idx_runs_coordinator_pane'
] as const

// Why versioned: idx_deliveries_one_outstanding/idx_deliveries_run_created/
// idx_questions_dispatch_status are created alongside the v8 `deliveries`/`question_threads`
// tables; idx_federation_relay_pending alongside v15's federation_relay_items;
// idx_remote_questions_dispatch_status alongside v16's remote_questions;
// idx_messages_delivery_contract alongside v19's messages.delivery_contract - all verified
// against migrate-v2-v12.ts/migrate-v13-v28.ts/migrate-legacy-contract-storage.ts, mirroring
// VERSIONED_POST_V6_COLUMNS's own fix.
// Why the rest (round 11, same follow-up-audit fix as the columns above): idx_dispatch_
// assignee_handle at v22 (see the v11/v28 column note above - same "createTables heals it
// first, never independently provable" caveat applies here too), idx_dispatch_active_assignee_
// handle at v25, idx_mutation_receipts_completed_updated alongside v26's mutation_receipt_ledger
// - all in migrate-v13-v28.ts.
// Why the 2 legacy-principal entries (round 12, fix for a real gap an independent review found
// on its own dedicated full re-audit): idx_legacy_principal_coordinator/idx_legacy_principal_
// dispatch are the ONLY enforcement of "at most one coordinator/worker principal per run" -
// legacy_compatibility_principals's own table-level UNIQUE(role, run_id, dispatch_id) does not
// cover the coordinator case, since SQLite treats every NULL dispatch_id as distinct. Missing
// them wouldn't misjudge completeness in a way that crashes, but would silently let
// getLegacyCompatibilityPrincipal's .get() pick an arbitrary one of several duplicates.
// Why round 13 also touched migrate-legacy-contract-storage.ts, not just this file: adding these
// 2 entries here changed a database with pre-existing duplicate principals from "boots
// degraded" to "rewinds to 6, then throws forever on this exact CREATE UNIQUE INDEX re-running
// into its own leftover duplicates" - migrateLegacyContractStorage now deletes duplicates
// (deterministic most-recent-wins) immediately before creating these indexes, so a completeness
// check that correctly diagnoses this state doesn't also make it unrepairable.
const VERSIONED_POST_V6_INDEXES = [
  { version: 8, index: 'idx_deliveries_one_outstanding' },
  { version: 8, index: 'idx_deliveries_run_created' },
  { version: 8, index: 'idx_questions_dispatch_status' },
  { version: 15, index: 'idx_federation_relay_pending' },
  { version: 16, index: 'idx_remote_questions_dispatch_status' },
  { version: 19, index: 'idx_messages_delivery_contract' },
  { version: 19, index: 'idx_legacy_principal_coordinator' },
  { version: 19, index: 'idx_legacy_principal_dispatch' },
  { version: 22, index: 'idx_dispatch_assignee_handle' },
  { version: 25, index: 'idx_dispatch_active_assignee_handle' },
  { version: 26, index: 'idx_mutation_receipts_completed_updated' }
] as const

function hasOrchestrationColumn(db: Database.Database, table: string, column: string): boolean {
  const rows = db.pragma(`table_info(${table})`) as { name: string }[]
  return rows.some((row) => row.name === column)
}

function hasOrchestrationIndex(db: Database.Database, index: string): boolean {
  return !!db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?").get(index)
}

function hasOrchestrationTrigger(db: Database.Database, trigger: string): boolean {
  return !!db
    .prepare("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?")
    .get(trigger)
}

function hasOrchestrationTable(db: Database.Database, table: string): boolean {
  return !!db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?").get(table)
}

// Why gated at v9 (round 10, fix for a real bug 2 independent reviews confirmed): the messages
// table only gains the 'question' CHECK-constraint member at v9 (migrate-v2-v12.ts's
// `if (current < 9 && !this.messagesTypeCheckAllowsQuestion())` rebuild) - calling this
// unconditionally judged every healthy v7-v8 database "incomplete" too.
function messagesAllowQuestions(db: Database.Database): boolean {
  const row = db
    .prepare("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'")
    .get() as { sql: string } | undefined
  return !!row && row.sql.includes("'question'")
}

// Why gated at v19, and why NOT just "wrongly judged incomplete" like the other probes (round 10,
// fix for a real bug 2 independent reviews confirmed): `legacy_adoptions` itself is created at
// v19 (migrate-legacy-contract-storage.ts, called from migrate-v13-v28.ts's
// `if (current < 19)`) - querying it unconditionally on a genuine pre-v19 database doesn't just
// misjudge completeness, it throws `no such table: legacy_adoptions`, uncaught, straight out of
// resolveOrchestrationMigrationStartVersion and through the whole OrchestrationDb constructor.
// This was masked as long as POST_V6_INDEXES stayed unconditional too (its own v19 entry,
// idx_messages_delivery_contract, already short-circuited `&&` before reaching this function for
// any pre-v19 database) - fixing the index list without ALSO gating this would have traded a
// spurious rewind for a startup crash. A database that hasn't reached v19 cannot have an
// inconsistent v19-era adoption record (the table doesn't exist yet), so treat it as trivially
// consistent rather than attempting the query at all.
function hasConsistentLegacyAdoption(db: Database.Database): boolean {
  const sourceRunId = 'run_legacy_local'
  const sourceGraph = db
    .prepare(
      `SELECT 1
       WHERE EXISTS(SELECT 1 FROM tasks WHERE run_id = ?)
          OR EXISTS(SELECT 1 FROM dispatch_contexts WHERE run_id = ?)
          OR EXISTS(SELECT 1 FROM decision_gates WHERE run_id = ?)
          OR EXISTS(SELECT 1 FROM messages WHERE run_id = ?)
          OR EXISTS(SELECT 1 FROM question_threads WHERE run_id = ?)
          OR EXISTS(SELECT 1 FROM deliveries WHERE run_id = ?)`
    )
    .get(sourceRunId, sourceRunId, sourceRunId, sourceRunId, sourceRunId, sourceRunId)
  const adoption = db
    .prepare('SELECT adopted_run_id FROM legacy_adoptions WHERE source_run_id = ?')
    .get(sourceRunId) as { adopted_run_id: string } | undefined
  if (sourceGraph) {
    return false
  }
  if (adoption) {
    return Boolean(
      db.prepare('SELECT 1 FROM runs WHERE id = ? AND legacy = 0').get(adoption.adopted_run_id)
    )
  }
  return true
}

// Why gated at v29, mirroring messagesAllowQuestions' v9 pattern (round 12, fix for a real gap
// an independent review found): tasks only gains 'cancelled'/'superseded' as CHECK-allowed
// statuses at v29 (migrate-task-terminal-states.ts's own rebuild, gated by migrate.ts's
// `if (current < 29)`), in the SAME rebuild that adds terminal_reason/replacement_task_id -
// those two columns are already probed above, but the CHECK constraint widening is a distinct
// fact this probe covers (migrateTaskTerminalStates's own idempotency guard,
// tasksStatusCheckAllowsCancelled, treats the CHECK as authoritative - the resolver should too).
function tasksAllowCancelledOrSuperseded(db: Database.Database): boolean {
  const row = db
    .prepare("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'")
    .get() as { sql: string } | undefined
  return !!row && row.sql.includes("'cancelled'")
}

// Why gated at v26 (round 12, fix for a real gap an independent review found): the v26 migration
// (migrateMutationReceiptCapacity) does 3 things in one exec() - creates mutation_receipt_ledger
// (already column-probed above), creates 2 triggers that keep receipt_count exact, and seeds the
// ledger's one row. Missing the triggers or the seed row wouldn't misjudge completeness in a way
// that crashes immediately, but ensureMutationReceiptCapacity's receiptCount() would eventually
// throw "Mutation receipt ledger metadata is missing" (seed row) or the count would silently
// drift and either never enforce the 10k cap or get permanently stuck reporting the ledger full
// (triggers) - both unrecoverable by restart, since a "complete" verdict here means migrate()
// never gets another chance to re-run this block.
// Why self-guarded, not relying on the sibling checks earlier in hasCompletePostV6Schema's &&
// chain (round 13, fix for a real gap an independent review found): this function's own SELECT
// would throw `no such table: mutation_receipt_ledger` on a database missing that table -
// currently unreachable only because 2 EARLIER, UNRELATED checks in the chain already catch that
// same database first (masking, not protecting). Repeating the exact mistake round 10 already
// fixed once for hasConsistentLegacyAdoption's `legacy_adoptions` query - probe the table's own
// existence before querying it, so this function is correct standing alone, not just by
// accident of evaluation order.
function hasConsistentMutationReceiptLedger(db: Database.Database): boolean {
  if (!hasOrchestrationTable(db, 'mutation_receipt_ledger')) {
    return false
  }
  return (
    hasOrchestrationTrigger(db, 'mutation_receipts_count_insert') &&
    hasOrchestrationTrigger(db, 'mutation_receipts_count_delete') &&
    !!db.prepare('SELECT 1 FROM mutation_receipt_ledger WHERE singleton = 1').get()
  )
}

function hasCompletePostV6Schema(db: Database.Database, storedVersion: number): boolean {
  return (
    POST_V6_COLUMNS.every(([table, column]) => hasOrchestrationColumn(db, table, column)) &&
    VERSIONED_POST_V6_COLUMNS.every(
      ({ version, table, column }) =>
        storedVersion < version || hasOrchestrationColumn(db, table, column)
    ) &&
    POST_V6_INDEXES.every((index) => hasOrchestrationIndex(db, index)) &&
    VERSIONED_POST_V6_INDEXES.every(
      ({ version, index }) => storedVersion < version || hasOrchestrationIndex(db, index)
    ) &&
    (storedVersion < 9 || messagesAllowQuestions(db)) &&
    (storedVersion < 19 || hasConsistentLegacyAdoption(db)) &&
    (storedVersion < 26 || hasConsistentMutationReceiptLedger(db)) &&
    (storedVersion < 29 || tasksAllowCancelledOrSuperseded(db))
  )
}

export function resolveOrchestrationMigrationStartVersion(
  db: Database.Database,
  storedVersion: number,
  schemaVersion: number
): number {
  if (storedVersion > schemaVersion) {
    return storedVersion
  }
  if (hasCompletePostV6Schema(db, storedVersion)) {
    return storedVersion
  }
  // Why: version-skewed pre-Run databases can claim the post-v6 range while retaining v6 tables.
  return Math.min(storedVersion, schemaVersion, 6)
}
