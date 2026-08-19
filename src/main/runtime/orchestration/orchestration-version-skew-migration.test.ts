import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import Database from '../../sqlite/sync-database'
import { SCHEMA_VERSION } from './db/contract-constants'
import { LEGACY_CONTRACT_VERSION, LEGACY_RUN_ID, OrchestrationDb } from './db'
import { resolveOrchestrationMigrationStartVersion } from './orchestration-schema-version-skew'

describe('OrchestrationDb version-skew migration', () => {
  let db: OrchestrationDb | undefined
  let tempDir: string | undefined

  afterEach(() => {
    db?.close()
    db = undefined
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true })
      tempDir = undefined
    }
  })

  function createLegacySchemaClaimingVersion(claimedVersion = 17): string {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const raw = new Database(dbPath)
    raw.exec(`
      CREATE TABLE messages (
        id TEXT NOT NULL,
        from_handle TEXT NOT NULL,
        to_handle TEXT NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        type TEXT NOT NULL DEFAULT 'status'
          CHECK(type IN (
            'status', 'dispatch', 'worker_done', 'merge_ready',
            'escalation', 'handoff', 'decision_gate', 'heartbeat'
          )),
        priority TEXT NOT NULL DEFAULT 'normal'
          CHECK(priority IN ('normal', 'high', 'urgent')),
        thread_id TEXT,
        payload TEXT,
        read INTEGER NOT NULL DEFAULT 0,
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        delivered_at TEXT,
        sender_pane_key TEXT
      );
      CREATE UNIQUE INDEX idx_messages_id ON messages(id);
      CREATE INDEX idx_inbox ON messages(to_handle, read);
      CREATE INDEX idx_thread ON messages(thread_id);

      CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        parent_id TEXT,
        created_by_terminal_handle TEXT,
        task_title TEXT,
        display_name TEXT,
        spec TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','ready','dispatched','completed','failed','blocked')),
        deps TEXT NOT NULL DEFAULT '[]',
        result TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at TEXT
      );
      CREATE INDEX idx_tasks_status ON tasks(status);
      CREATE INDEX idx_tasks_parent ON tasks(parent_id);

      CREATE TABLE dispatch_contexts (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        assignee_handle TEXT,
        assignee_pane_key TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','dispatched','completed','failed','circuit_broken')),
        failure_count INTEGER NOT NULL DEFAULT 0,
        last_failure TEXT,
        dispatched_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_heartbeat_at TEXT
      );
      CREATE INDEX idx_dispatch_task ON dispatch_contexts(task_id);
      CREATE INDEX idx_dispatch_status ON dispatch_contexts(status);

      CREATE TABLE decision_gates (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','resolved','timeout')),
        resolution TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at TEXT
      );
      CREATE INDEX idx_gates_task ON decision_gates(task_id);
      CREATE INDEX idx_gates_status ON decision_gates(status);

      CREATE TABLE coordinator_runs (
        id TEXT PRIMARY KEY,
        spec TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'idle'
          CHECK(status IN ('idle','running','completed','failed')),
        coordinator_handle TEXT NOT NULL,
        poll_interval_ms INTEGER NOT NULL DEFAULT 2000,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at TEXT
      );

      INSERT INTO messages (
        id, from_handle, to_handle, subject, body, type
      ) VALUES (
        'msg_legacy', 'term_worker', 'term_coord', 'retained message', 'done', 'status'
      );
      INSERT INTO tasks (
        id, created_by_terminal_handle, task_title, display_name, spec, status
      ) VALUES (
        'task_legacy', 'term_coord', 'Legacy task', 'Legacy task', 'retained task', 'dispatched'
      );
      INSERT INTO dispatch_contexts (
        id, task_id, assignee_handle, assignee_pane_key, status
      ) VALUES (
        'ctx_legacy', 'task_legacy', 'term_worker', 'tab_legacy:leaf_legacy', 'dispatched'
      );
      INSERT INTO decision_gates (
        id, task_id, question
      ) VALUES (
        'gate_legacy', 'task_legacy', 'retained gate'
      );
    `)
    raw.pragma(`user_version = ${claimedVersion}`)
    raw.close()
    return dbPath
  }

  it('repairs retained v6 rows when the database already claims v17', () => {
    const dbPath = createLegacySchemaClaimingVersion()
    db = new OrchestrationDb(dbPath)

    const adoptedRunId = db.getLegacyAdoption()?.adopted_run_id
    expect(adoptedRunId).toBeTruthy()
    expect(db.getRun(LEGACY_RUN_ID)).toMatchObject({ legacy: 1 })
    expect(db.getMessageById('msg_legacy')).toMatchObject({
      run_id: adoptedRunId,
      delivery_contract: 'legacy_direct'
    })
    expect(db.getTask('task_legacy')).toMatchObject({ run_id: adoptedRunId })
    expect(db.getDispatchContextById('ctx_legacy')).toMatchObject({
      run_id: adoptedRunId,
      contract_version: LEGACY_CONTRACT_VERSION
    })
    expect(db.getGate('gate_legacy')).toMatchObject({ run_id: adoptedRunId })

    const run = db.createRun({
      objective: 'verify repaired orchestration',
      coordinatorHandle: 'term_coord_v2',
      coordinatorPaneKey: 'tab_v2:leaf_coord'
    })
    const task = db.createTask({ spec: 'reply with ack', runId: run.id })
    const dispatch = db.createDispatchContext(task.id, 'term_worker_v2', 'tab_v2:leaf_worker')
    const question = db.createQuestion({
      runId: run.id,
      dispatchId: dispatch.id,
      askerHandle: 'term_worker_v2',
      question: 'ack?'
    })
    const delivery = db.getOrCreateRunDelivery({
      runId: run.id,
      consumerGeneration: run.consumer_generation
    })
    expect(question.message.type).toBe('question')
    expect(delivery?.messages.map((message) => message.id)).toContain(question.message.id)

    db.close()
    db = undefined
    db = new OrchestrationDb(dbPath)
    expect(db.listTasks({ runId: adoptedRunId }).map((row) => row.id)).toEqual(['task_legacy'])
    expect(db.getRun(run.id)).toBeDefined()
    expect(db.getQuestion(question.message.id)).toMatchObject({ status: 'pending' })
  })

  it('does not repair an incomplete schema written by a future binary', () => {
    const dbPath = createLegacySchemaClaimingVersion(20)
    const raw = new Database(dbPath)

    expect(resolveOrchestrationMigrationStartVersion(raw, 20, 19)).toBe(20)
    expect(raw.pragma('user_version', { simple: true })).toBe(20)

    raw.close()
  })

  // Why (#14548 round 7 P2, fix for a real regression a review found): the three tasks.created_by_*
  // columns land at v24 (migrate-v13-v28.ts's `if (current < 24)` block) - putting them in the
  // UNVERSIONED POST_V6_COLUMNS list instead of VERSIONED_POST_V6_COLUMNS made a perfectly
  // healthy pre-v24 database (v7-v23, which never needed these columns) look "incomplete" and
  // rewound it all the way back to migration start version 6, unnecessarily re-running every
  // migration since v7 including several live-data backfills.
  it('does not rewind a healthy pre-v24 database missing only the v24 created_by_* columns', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-pre-v24-'))
    const dbPath = join(tempDir, 'orchestration.db')
    // Seed a fully-current, fully-migrated database, then strip it back down to what a real
    // pre-v24 database actually looked like - present through v23, missing only the v24 columns.
    const seed = new OrchestrationDb(dbPath)
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('ALTER TABLE tasks DROP COLUMN created_by_pane_key')
    raw.exec('ALTER TABLE tasks DROP COLUMN created_by_process_incarnation')
    raw.exec('ALTER TABLE tasks DROP COLUMN created_by_run_generation')
    raw.pragma('user_version = 23')

    expect(resolveOrchestrationMigrationStartVersion(raw, 23, SCHEMA_VERSION)).toBe(23)

    raw.close()
  })

  // Why (#14548 round 9, fix for the SAME regression class found pre-existing in nearly every
  // other originally-unversioned column, not just the 3 round 7 fixed): question_threads.run_id
  // (v8), the dispatch_contexts capability trio (v10), worker_dispatches.runtime_epoch (v13),
  // the federation/relay columns and table (v15), remote_questions (v16),
  // remote_dispatch_attachments.protocol_version (v17), and the legacy-contract-storage columns
  // and tables (v19) were ALL unversioned, so a healthy database at any intermediate version
  // (e.g. v9, genuinely missing the v10 capability trio because it hasn't been migrated that far
  // yet) looked "incomplete" and got rewound to 6.
  // Why the index drop too (round 10, fix for an independent review's finding that THIS test was
  // a false positive): only dropping columns left every POST_V6_INDEXES entry present (all 11
  // were still unconditional when this test was first written, but a v9 DB - via createTables
  // building a FULL current schema first before this seed-then-strip approach even ran - had them
  // all anyway), so the test passed for the wrong reason and never actually exercised the index
  // gate at all. `idx_messages_delivery_contract` (v19) is the specific index a genuine pre-v19
  // database can never have - drop it explicitly so this test would fail if that gate ever
  // regressed back to unconditional.
  it('does not rewind a healthy pre-v10 database missing the v10 dispatch capability columns and later indexes', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-pre-v10-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('ALTER TABLE dispatch_contexts DROP COLUMN capability_hash')
    raw.exec('ALTER TABLE dispatch_contexts DROP COLUMN process_incarnation')
    raw.exec('ALTER TABLE dispatch_contexts DROP COLUMN capability_revoked_at')
    raw.exec('DROP INDEX idx_messages_delivery_contract')
    raw.exec('DROP INDEX idx_federation_relay_pending')
    raw.exec('DROP INDEX idx_remote_questions_dispatch_status')
    raw.pragma('user_version = 9')

    expect(resolveOrchestrationMigrationStartVersion(raw, 9, SCHEMA_VERSION)).toBe(9)

    raw.close()
  })

  // Why (round 10, fix for a real bug 2 independent reviews confirmed): hasConsistentLegacyAdoption
  // queries the v19 `legacy_adoptions` table with no existence guard - a genuine pre-v19 database
  // doesn't have that table at all, so calling this unconditionally doesn't just misjudge
  // completeness, it THROWS ("no such table: legacy_adoptions") straight out of
  // resolveOrchestrationMigrationStartVersion. This was masked as long as an unversioned
  // POST_V6_INDEXES entry (idx_messages_delivery_contract, also v19) short-circuited first for
  // any pre-v19 database - fixing the index gate without ALSO gating this function would have
  // traded a spurious rewind for an uncaught constructor crash.
  it('does not throw (or rewind) for a healthy pre-v19 database with no legacy_adoptions table at all', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-pre-v19-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('DROP TABLE legacy_adoptions')
    raw.exec('DROP TABLE legacy_compatibility_principals')
    raw.exec('DROP TABLE legacy_operation_receipts')
    raw.exec('DROP TABLE legacy_mail_receipts')
    raw.exec('DROP INDEX idx_messages_delivery_contract')
    raw.pragma('user_version = 18')

    expect(() => resolveOrchestrationMigrationStartVersion(raw, 18, SCHEMA_VERSION)).not.toThrow()
    expect(resolveOrchestrationMigrationStartVersion(raw, 18, SCHEMA_VERSION)).toBe(18)

    raw.close()
  })

  // Why (round 11): the v9 boundary (messagesAllowQuestions) can't use the generic drop-a-
  // column/index matrix below - it's a CHECK constraint, not a probeable column, so simulating
  // "a real v8 database" means rebuilding messages with the pre-v9 constraint that excludes
  // 'question', mirroring what migrate-v2-v12.ts's own v9 block actually rebuilds FROM.
  it('does not rewind a healthy v8 database whose messages CHECK constraint predates the v9 question type', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-pre-v9-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    seed.close()
    const raw = new Database(dbPath)
    raw.exec(`
      CREATE TABLE messages_pre_v9 (
        id            TEXT NOT NULL,
        run_id        TEXT NOT NULL,
        from_handle   TEXT NOT NULL,
        to_handle     TEXT NOT NULL,
        subject       TEXT NOT NULL,
        body          TEXT NOT NULL DEFAULT '',
        type          TEXT NOT NULL DEFAULT 'status'
          CHECK(type IN (
            'status', 'dispatch', 'worker_done', 'merge_ready',
            'escalation', 'handoff', 'decision_gate', 'heartbeat'
          )),
        priority      TEXT NOT NULL DEFAULT 'normal'
          CHECK(priority IN ('normal', 'high', 'urgent')),
        thread_id     TEXT,
        payload       TEXT,
        read          INTEGER NOT NULL DEFAULT 0,
        sequence      INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        delivered_at  TEXT,
        sender_pane_key TEXT
      );
      INSERT INTO messages_pre_v9 (
        id, run_id, from_handle, to_handle, subject, body, type, priority,
        thread_id, payload, read, sequence, created_at, delivered_at, sender_pane_key
      )
      SELECT
        id, run_id, from_handle, to_handle, subject, body, type, priority,
        thread_id, payload, read, sequence, created_at, delivered_at, sender_pane_key
      FROM messages;
      DROP TABLE messages;
      ALTER TABLE messages_pre_v9 RENAME TO messages;
      CREATE UNIQUE INDEX idx_messages_id ON messages(id);
      CREATE INDEX idx_inbox ON messages(to_handle, read);
      CREATE INDEX idx_thread ON messages(thread_id);
      CREATE INDEX idx_messages_run_sequence ON messages(run_id, sequence);
    `)
    raw.pragma('user_version = 8')

    expect(resolveOrchestrationMigrationStartVersion(raw, 8, SCHEMA_VERSION)).toBe(8)

    raw.close()
  })

  // Why (round 11, fix for a real bug a genuinely-independent review found beyond what round
  // 9/10's own sweeps covered): round 9's sweep only grepped 3 migration files and stopped at
  // v27; it never checked migrate.ts itself (which has its own inline v29-v31 blocks, not
  // delegated to migrate-v13-v28.ts despite that file's name) nor v11's/v22's/v25's/v28's
  // artifacts. A full version-by-version matrix - not just a couple of spot-checks - is what
  // actually catches this class of gap, since each version's structures are independent and a
  // spot-check only proves the versions it happens to pick. Every version boundary this file's
  // VERSIONED_POST_V6_COLUMNS/VERSIONED_POST_V6_INDEXES lists reference gets its own case here:
  // strip back a fully-current database to exactly what a real database AT that version (one
  // version below the boundary) would have, and confirm the resolver trusts it instead of
  // rewinding to 6.
  const VERSION_BOUNDARY_DROPS: {
    version: number
    columns?: [string, string][]
    indexes?: string[]
    tables?: string[]
  }[] = [
    {
      version: 8,
      columns: [['question_threads', 'run_id']],
      indexes: [
        'idx_deliveries_one_outstanding',
        'idx_deliveries_run_created',
        'idx_questions_dispatch_status'
      ]
    },
    {
      version: 10,
      columns: [
        ['dispatch_contexts', 'capability_hash'],
        ['dispatch_contexts', 'process_incarnation'],
        ['dispatch_contexts', 'capability_revoked_at']
      ]
    },
    // Why table drops, not column drops: message_id/singleton/transport are each their table's
    // PRIMARY KEY, which SQLite refuses to DROP COLUMN - dropping the whole table simulates "this
    // table doesn't exist yet" just as accurately (hasOrchestrationColumn returns false either way).
    { version: 11, tables: ['mutation_receipts'] },
    { version: 13, columns: [['worker_dispatches', 'runtime_epoch']] },
    {
      version: 15,
      columns: [
        ['federated_dispatches', 'to_home_imported_sequence'],
        ['remote_dispatch_attachments', 'to_worker_imported_sequence']
      ],
      indexes: ['idx_federation_relay_pending']
    },
    {
      version: 16,
      tables: ['remote_questions'],
      indexes: ['idx_remote_questions_dispatch_status']
    },
    { version: 17, columns: [['remote_dispatch_attachments', 'protocol_version']] },
    {
      version: 19,
      columns: [
        ['messages', 'delivery_contract'],
        ['coordinator_runs', 'scheduler_lost_at'],
        ['dispatch_contexts', 'contract_version'],
        ['dispatch_contexts', 'launch_token_hash']
      ],
      // Why these 4 extra indexes: all reference messages.delivery_contract in their own
      // definition (createMailboxDeliveryIndexesIfPossible only creates them once that column
      // exists) - SQLite refuses to drop a column an index still references, so they have to go
      // first. A genuine pre-v19 database never has any of them, for the same reason.
      indexes: [
        'idx_messages_delivery_contract',
        'idx_messages_undelivered_direct_run',
        'idx_messages_unread_current_inbox',
        'idx_messages_unread_current_inbox_type',
        'idx_messages_unread_current_run_type'
      ]
    },
    { version: 22, indexes: ['idx_dispatch_assignee_handle'] },
    {
      version: 24,
      columns: [
        ['tasks', 'created_by_pane_key'],
        ['tasks', 'created_by_process_incarnation'],
        ['tasks', 'created_by_run_generation']
      ]
    },
    { version: 25, indexes: ['idx_dispatch_active_assignee_handle'] },
    {
      version: 26,
      tables: ['mutation_receipt_ledger'],
      indexes: ['idx_mutation_receipts_completed_updated']
    },
    { version: 27, columns: [['federated_dispatches', 'to_home_acknowledged_sequence']] },
    { version: 28, tables: ['mutation_caller_identities'] },
    {
      version: 29,
      columns: [
        ['tasks', 'terminal_reason'],
        ['tasks', 'replacement_task_id']
      ]
    },
    { version: 30, columns: [['worker_dispatches', 'terminated_by']] },
    { version: 31, columns: [['dispatch_contexts', 'stale_escalated_at']] }
  ]

  it.each(VERSION_BOUNDARY_DROPS)(
    'does not rewind a healthy database exactly one version below the v$version boundary',
    ({ version, columns, indexes, tables }) => {
      tempDir = mkdtempSync(join(tmpdir(), `orca-db-version-skew-pre-v${version}-`))
      const dbPath = join(tempDir, 'orchestration.db')
      const seed = new OrchestrationDb(dbPath)
      seed.close()
      const raw = new Database(dbPath)
      // Why indexes/triggers before columns: anything whose definition references a column
      // blocks that column's DROP COLUMN until it's gone too - messages.delivery_contract is
      // also read by a trigger, not just indexes.
      for (const index of indexes ?? []) {
        raw.exec(`DROP INDEX ${index}`)
      }
      for (const trigger of raw
        .prepare("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        .all() as { name: string }[]) {
        raw.exec(`DROP TRIGGER ${trigger.name}`)
      }
      for (const [table, column] of columns ?? []) {
        raw.exec(`ALTER TABLE ${table} DROP COLUMN ${column}`)
      }
      for (const table of tables ?? []) {
        raw.exec(`DROP TABLE ${table}`)
      }
      const storedVersion = version - 1
      raw.pragma(`user_version = ${storedVersion}`)

      expect(() =>
        resolveOrchestrationMigrationStartVersion(raw, storedVersion, SCHEMA_VERSION)
      ).not.toThrow()
      expect(resolveOrchestrationMigrationStartVersion(raw, storedVersion, SCHEMA_VERSION)).toBe(
        storedVersion
      )

      raw.close()
    }
  )
})
