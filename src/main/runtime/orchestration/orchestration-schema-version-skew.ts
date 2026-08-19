import type Database from '../../sqlite/sync-database'

// Why (round 9, fix for a systemic issue an independent review surfaced then a full sweep of
// every entry against migrate-v2-v12.ts/migrate-v13-v28.ts/migrate-legacy-contract-storage.ts
// confirmed): this list must hold ONLY columns present as of the TRUE v7 baseline (the first
// migration past the original v6 schema) - anything added by a LATER migration belongs in
// VERSIONED_POST_V6_COLUMNS below instead, or a healthy database at any version between v7 and
// that column's real introduction gets wrongly judged "incomplete" and rewound to 6. Verified:
// only these 4 - plus the 2 already-correct `run_id` entries used in the v7 backfill loop -
// actually land at v7 (`if (current < 7)` in migrate-v2-v12.ts); every other entry that used to
// be here turned out to be a later addition and has been moved down.
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
  { version: 27, table: 'federated_dispatches', column: 'to_home_acknowledged_sequence' }
] as const

// Why NOT fixed this round (flagging, not silently leaving): this list is checked
// unconditionally, same shape as POST_V6_COLUMNS before round 9's fix - by inspection,
// idx_messages_delivery_contract/idx_deliveries_one_outstanding/idx_deliveries_run_created/
// idx_questions_dispatch_status/idx_federation_relay_pending/idx_remote_questions_dispatch_status
// look like they were likely introduced alongside the same later-version tables/columns above
// (v8's question_threads/deliveries, v15's federation_relay_items, v16's remote_questions), which
// would make them subject to the identical false-incomplete-rewind bug for a healthy database at
// an intermediate version. NOT independently verified against the migration files the way every
// POST_V6_COLUMNS entry above was this round - a real candidate for the next pass, not confirmed.
const POST_V6_INDEXES = [
  'idx_messages_run_sequence',
  'idx_messages_delivery_contract',
  'idx_tasks_run_status',
  'idx_dispatch_run_status',
  'idx_gates_run_status',
  'idx_runs_coordinator_pane',
  'idx_deliveries_one_outstanding',
  'idx_deliveries_run_created',
  'idx_questions_dispatch_status',
  'idx_federation_relay_pending',
  'idx_remote_questions_dispatch_status'
] as const

function hasOrchestrationColumn(db: Database.Database, table: string, column: string): boolean {
  const rows = db.pragma(`table_info(${table})`) as { name: string }[]
  return rows.some((row) => row.name === column)
}

function hasOrchestrationIndex(db: Database.Database, index: string): boolean {
  return !!db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?").get(index)
}

function messagesAllowQuestions(db: Database.Database): boolean {
  const row = db
    .prepare("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'")
    .get() as { sql: string } | undefined
  return !!row && row.sql.includes("'question'")
}

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

function hasCompletePostV6Schema(db: Database.Database, storedVersion: number): boolean {
  return (
    POST_V6_COLUMNS.every(([table, column]) => hasOrchestrationColumn(db, table, column)) &&
    VERSIONED_POST_V6_COLUMNS.every(
      ({ version, table, column }) =>
        storedVersion < version || hasOrchestrationColumn(db, table, column)
    ) &&
    POST_V6_INDEXES.every((index) => hasOrchestrationIndex(db, index)) &&
    messagesAllowQuestions(db) &&
    hasConsistentLegacyAdoption(db)
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
