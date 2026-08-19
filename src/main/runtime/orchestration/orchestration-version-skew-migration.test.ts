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

  // Why (round 12): the v26 mutation-receipt-ledger consistency probe checks 2 triggers + a seed
  // row, none of which the generic column/index/table matrix below can exercise (dropping only
  // the ledger's `singleton` column, as that matrix does for the "v26 boundary" case, leaves the
  // triggers and seed row untouched - it never proves THIS probe does anything). Test each half
  // independently: missing triggers alone, and a missing seed row alone, both while claiming the
  // current schema version, must both be caught.
  it.each(['triggers', 'seed row'] as const)(
    'rewinds a database claiming the current version whose v26 mutation-receipt ledger is missing its %s',
    (missing) => {
      tempDir = mkdtempSync(
        join(tmpdir(), `orca-db-version-skew-v26-${missing.replace(' ', '-')}-`)
      )
      const dbPath = join(tempDir, 'orchestration.db')
      const seed = new OrchestrationDb(dbPath)
      seed.close()
      const raw = new Database(dbPath)
      if (missing === 'triggers') {
        raw.exec('DROP TRIGGER mutation_receipts_count_insert')
        raw.exec('DROP TRIGGER mutation_receipts_count_delete')
      } else {
        raw.exec('DELETE FROM mutation_receipt_ledger WHERE singleton = 1')
      }
      raw.pragma(`user_version = ${SCHEMA_VERSION}`)

      expect(resolveOrchestrationMigrationStartVersion(raw, SCHEMA_VERSION, SCHEMA_VERSION)).toBe(6)

      raw.close()
    }
  )

  it('does not rewind a healthy v25 database missing only the v26 mutation-receipt ledger entirely', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-pre-v26-ledger-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('DROP TRIGGER mutation_receipts_count_insert')
    raw.exec('DROP TRIGGER mutation_receipts_count_delete')
    raw.exec('DROP INDEX idx_mutation_receipts_completed_updated')
    raw.exec('DROP TABLE mutation_receipt_ledger')
    raw.pragma('user_version = 25')

    expect(resolveOrchestrationMigrationStartVersion(raw, 25, SCHEMA_VERSION)).toBe(25)

    raw.close()
  })

  // Why (round 13, fix for a real regression an independent review found and reproduced): the
  // v19 legacy-principal unique indexes round 12 added to the completeness check changed the
  // failure mode of an ALREADY-corrupted database (duplicate coordinator principals for one run,
  // only reachable via external corruption/repair tooling, never via normal writes) from
  // "boots in a silently-degraded state" to "throws permanently on every future boot" - because
  // once the resolver correctly diagnoses the database as incomplete and rewinds to 6,
  // migrateLegacyContractStorage's own CREATE UNIQUE INDEX hits the duplicates it exists to
  // prevent. A completeness check that correctly diagnoses a repairable state must not also make
  // it unrepairable - opening a real OrchestrationDb (not just calling the resolver in
  // isolation) must both NOT throw and actually repair the duplicate down to one row.
  it('repairs (does not crash on) a database with duplicate legacy coordinator principals', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-dup-coordinator-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    const run = seed.createRun({
      objective: 'dup coordinator repro',
      coordinatorHandle: 'term_coord',
      coordinatorPaneKey: 'tab_coord:11111111-1111-4111-8111-111111111111'
    })
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('DROP INDEX idx_legacy_principal_coordinator')
    const insertPrincipal = raw.prepare(
      `INSERT INTO legacy_compatibility_principals (
         id, run_id, dispatch_id, role, host_scope, terminal_handle, pane_key,
         launch_token_hash, process_incarnation, status
       ) VALUES (?, ?, NULL, 'coordinator', '{}', ?, ?, ?, NULL, 'committed')`
    )
    insertPrincipal.run('legacy_p_1', run.id, 'term_coord_a', 'tab_a:leaf_a', 'hash_a')
    insertPrincipal.run('legacy_p_2', run.id, 'term_coord_b', 'tab_b:leaf_b', 'hash_b')
    raw.pragma(`user_version = ${SCHEMA_VERSION}`)
    raw.close()

    expect(() => {
      db = new OrchestrationDb(dbPath)
    }).not.toThrow()

    const rawAfter = (db as unknown as { db: Database.Database }).db
    const remaining = rawAfter
      .prepare("SELECT id FROM legacy_compatibility_principals WHERE role = 'coordinator'")
      .all() as { id: string }[]
    expect(remaining).toHaveLength(1)
    // Why the surviving row: neither duplicate's terminal_handle matches the run's live
    // coordinator_handle ('term_coord'), so the round-15 authoritative-match step is a no-op here
    // and this falls through to the highest-rowid tie-break - legacy_p_2 was inserted after
    // legacy_p_1.
    expect(remaining[0]?.id).toBe('legacy_p_2')
  })

  // Why (round 14, fix for a real bug an independent review found and reproduced): a plain
  // highest-rowid tie-break could keep a 'revoked' duplicate over a 'committed' one - the one
  // status that is BY DEFINITION unrepairable (commitLegacyCompatibilityPrincipal permanently
  // refuses a revoked principal), directly undermining round 13's own "must not make a
  // repairable state unrepairable" fix. The revoked row here has the HIGHER rowid (inserted
  // second) specifically to prove the fix doesn't just fall back to highest-rowid regardless.
  // Why the run's coordinator_handle is cleared to NULL below (round 16 correction, an
  // independent review found the original seed - a handle unrelated to either duplicate - had
  // silently become a round-16 repro instead of a round-14 one): round 16 added a tier that
  // INVERTS this preference specifically when the run has a live, non-NULL coordinator_handle
  // that matches neither duplicate (every duplicate is then known-stale, so prefer 'revoked' -
  // see that tier's own comment). This test exists to isolate round 14's still-real fallback
  // tier - genuinely no live-binding information at all - not round 16's; a separate dedicated
  // test below covers round 16's inverted case.
  it('prefers a non-revoked legacy coordinator principal over a revoked one when deduping', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-dup-coordinator-revoked-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    const run = seed.createRun({
      objective: 'dup coordinator revoked-tiebreak repro',
      coordinatorHandle: 'term_coord',
      coordinatorPaneKey: 'tab_coord:22222222-2222-4222-9222-222222222222'
    })
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('DROP INDEX idx_legacy_principal_coordinator')
    raw.prepare('UPDATE runs SET coordinator_handle = NULL WHERE id = ?').run(run.id)
    const insertPrincipal = raw.prepare(
      `INSERT INTO legacy_compatibility_principals (
         id, run_id, dispatch_id, role, host_scope, terminal_handle, pane_key,
         launch_token_hash, process_incarnation, status
       ) VALUES (?, ?, NULL, 'coordinator', '{}', ?, ?, ?, NULL, ?)`
    )
    insertPrincipal.run(
      'legacy_p_committed',
      run.id,
      'term_coord_committed',
      'tab_committed:leaf',
      'hash_committed',
      'committed'
    )
    // Inserted AFTER (so it would win a plain highest-rowid tie-break) but revoked.
    insertPrincipal.run(
      'legacy_p_revoked',
      run.id,
      'term_coord_revoked',
      'tab_revoked:leaf',
      'hash_revoked',
      'revoked'
    )
    raw.pragma(`user_version = ${SCHEMA_VERSION}`)
    raw.close()

    db = new OrchestrationDb(dbPath)

    const rawAfter = (db as unknown as { db: Database.Database }).db
    const remaining = rawAfter
      .prepare("SELECT id, status FROM legacy_compatibility_principals WHERE role = 'coordinator'")
      .all() as { id: string; status: string }[]
    expect(remaining).toHaveLength(1)
    expect(remaining[0]).toMatchObject({ id: 'legacy_p_committed', status: 'committed' })
  })

  // Why (round 15, fix for a real design gap an independent review found in round 14's own fix):
  // 'revoked' is not a corruption marker - it is the coordinator's genuine terminal state after a
  // legitimate takeover, so preferring "non-revoked" by status alone has no guaranteed
  // correlation with which duplicate the run still actually recognizes. The one signal that IS
  // authoritative is which duplicate's terminal_handle matches the run's live coordinator_handle.
  // This seeds the OPPOSITE shape from round 14's own test: the REVOKED row here is the one whose
  // terminal_handle matches the run's live coordinator_handle, and it is also the LOWER-rowid row
  // (inserted first) - so both the round-14 status-preference heuristic AND a plain rowid
  // tie-break would each independently pick the wrong (committed, non-matching) row. Only
  // consulting the live binding gets this right.
  it('prefers a duplicate whose terminal_handle matches the run coordinator_handle, even if revoked', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-dup-coordinator-live-binding-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    const run = seed.createRun({
      objective: 'dup coordinator live-binding repro',
      coordinatorHandle: 'term_coord_live',
      coordinatorPaneKey: 'tab_coord:33333333-3333-4333-9333-333333333333'
    })
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('DROP INDEX idx_legacy_principal_coordinator')
    const insertPrincipal = raw.prepare(
      `INSERT INTO legacy_compatibility_principals (
         id, run_id, dispatch_id, role, host_scope, terminal_handle, pane_key,
         launch_token_hash, process_incarnation, status
       ) VALUES (?, ?, NULL, 'coordinator', '{}', ?, ?, ?, NULL, ?)`
    )
    // Inserted FIRST (lower rowid) and revoked - but its terminal_handle matches the run's live
    // coordinator_handle, so it is the one that should survive.
    insertPrincipal.run(
      'legacy_p_live_revoked',
      run.id,
      'term_coord_live',
      'tab_live:leaf',
      'hash_live',
      'revoked'
    )
    // Inserted SECOND (higher rowid) and committed - but stale: no longer the run's coordinator.
    insertPrincipal.run(
      'legacy_p_stale_committed',
      run.id,
      'term_coord_stale',
      'tab_stale:leaf',
      'hash_stale',
      'committed'
    )
    raw.pragma(`user_version = ${SCHEMA_VERSION}`)
    raw.close()

    db = new OrchestrationDb(dbPath)

    const rawAfter = (db as unknown as { db: Database.Database }).db
    const remaining = rawAfter
      .prepare("SELECT id, status FROM legacy_compatibility_principals WHERE role = 'coordinator'")
      .all() as { id: string; status: string }[]
    expect(remaining).toHaveLength(1)
    expect(remaining[0]).toMatchObject({ id: 'legacy_p_live_revoked', status: 'revoked' })
  })

  // Why (round 15): mirrors the coordinator case above for the worker/dispatch tier - the
  // authoritative signal there is the dispatch's live assignee_handle, not principal status.
  it('prefers a duplicate legacy worker principal whose terminal_handle matches the dispatch assignee_handle', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-dup-worker-live-binding-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    const run = seed.createRun({
      objective: 'dup worker live-binding repro',
      coordinatorHandle: 'term_coord',
      coordinatorPaneKey: 'tab_coord:66666666-6666-4666-9666-666666666666'
    })
    const task = seed.createTask({ spec: 'legacy worker dup repro', runId: run.id })
    const dispatch = seed.createDispatchContext(task.id, 'term_worker_live', 'tab_worker:leaf')
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('DROP INDEX idx_legacy_principal_dispatch')
    // Unlike the coordinator case (NULL dispatch_id makes every row distinct under the table's
    // own UNIQUE(role, run_id, dispatch_id)), two worker rows for the SAME dispatch_id collide
    // with that base constraint too, and SQLite refuses to drop a UNIQUE constraint's
    // auto-generated index directly - rebuild the (still-empty at this point) table without it,
    // simulating a database whose table predates that constraint.
    raw.exec(`
      DROP TABLE legacy_compatibility_principals;
      CREATE TABLE legacy_compatibility_principals (
        id                  TEXT PRIMARY KEY,
        run_id              TEXT NOT NULL,
        dispatch_id         TEXT,
        role                TEXT NOT NULL CHECK(role IN ('worker', 'coordinator')),
        host_scope          TEXT NOT NULL,
        terminal_handle     TEXT NOT NULL,
        pane_key            TEXT NOT NULL,
        launch_token_hash   TEXT NOT NULL,
        process_incarnation TEXT,
        status              TEXT NOT NULL CHECK(status IN ('committed', 'settled', 'revoked')),
        CHECK(
          (role = 'worker' AND dispatch_id IS NOT NULL) OR
          (role = 'coordinator' AND dispatch_id IS NULL)
        )
      );
    `)
    const insertPrincipal = raw.prepare(
      `INSERT INTO legacy_compatibility_principals (
         id, run_id, dispatch_id, role, host_scope, terminal_handle, pane_key,
         launch_token_hash, process_incarnation, status
       ) VALUES (?, ?, ?, 'worker', '{}', ?, ?, ?, NULL, ?)`
    )
    // Inserted FIRST (lower rowid) and settled - its terminal_handle matches the dispatch's live
    // assignee_handle, so it is the one that should survive. A plain highest-rowid tie-break
    // would pick the OTHER row instead, so this genuinely isolates the authoritative-match step.
    insertPrincipal.run(
      'legacy_p_worker_live_settled',
      run.id,
      dispatch.id,
      'term_worker_live',
      'tab_live:leaf',
      'hash_live',
      'settled'
    )
    // Inserted SECOND (higher rowid) and committed - but stale: no longer the dispatch's assignee.
    insertPrincipal.run(
      'legacy_p_worker_stale_committed',
      run.id,
      dispatch.id,
      'term_worker_stale',
      'tab_stale:leaf',
      'hash_stale',
      'committed'
    )
    raw.pragma(`user_version = ${SCHEMA_VERSION}`)
    raw.close()

    db = new OrchestrationDb(dbPath)

    const rawAfter = (db as unknown as { db: Database.Database }).db
    const remaining = rawAfter
      .prepare("SELECT id, status FROM legacy_compatibility_principals WHERE role = 'worker'")
      .all() as { id: string; status: string }[]
    expect(remaining).toHaveLength(1)
    expect(remaining[0]).toMatchObject({ id: 'legacy_p_worker_live_settled', status: 'settled' })
  })

  // Why (round 16, fix for a real P2 an independent review found in round 15's own fix): round
  // 15's authoritative-match tier is a no-op in the MOST common post-takeover shape - a run's
  // live coordinator_handle after a genuine handoff belongs to the NEW coordinator, which never
  // appears as a legacy principal's terminal_handle at all (confirmed empirically: the new
  // coordinator never gets a legacy_compatibility_principals row of its own). When the run HAS a
  // live coordinator_handle but it matches NEITHER duplicate, every duplicate is definitively
  // stale - in that case, prefer 'revoked' over 'committed' (the OPPOSITE of round 14's default),
  // because a surviving 'committed' row would let a stale legacy identity still authenticate via
  // commitLegacyCompatibilityPrincipal's existing-row match and exert live legacy operations
  // against a run it no longer has authority over.
  it('prefers a revoked legacy coordinator principal over a committed one when the run coordinator_handle matches neither', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-dup-coordinator-known-stale-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    const run = seed.createRun({
      objective: 'dup coordinator known-stale repro',
      coordinatorHandle: 'term_coord_modern',
      coordinatorPaneKey: 'tab_coord:77777777-7777-4777-9777-777777777777'
    })
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('DROP INDEX idx_legacy_principal_coordinator')
    const insertPrincipal = raw.prepare(
      `INSERT INTO legacy_compatibility_principals (
         id, run_id, dispatch_id, role, host_scope, terminal_handle, pane_key,
         launch_token_hash, process_incarnation, status
       ) VALUES (?, ?, NULL, 'coordinator', '{}', ?, ?, ?, NULL, ?)`
    )
    // Inserted FIRST (lower rowid) and committed - would win both round 14's status preference
    // AND a plain highest-rowid tie-break, so this genuinely isolates the round-16 inversion.
    insertPrincipal.run(
      'legacy_p_known_stale_committed',
      run.id,
      'term_coord_old_a',
      'tab_a:leaf',
      'hash_a',
      'committed'
    )
    insertPrincipal.run(
      'legacy_p_known_stale_revoked',
      run.id,
      'term_coord_old_b',
      'tab_b:leaf',
      'hash_b',
      'revoked'
    )
    raw.pragma(`user_version = ${SCHEMA_VERSION}`)
    raw.close()

    db = new OrchestrationDb(dbPath)

    const rawAfter = (db as unknown as { db: Database.Database }).db
    const remaining = rawAfter
      .prepare("SELECT id, status FROM legacy_compatibility_principals WHERE role = 'coordinator'")
      .all() as { id: string; status: string }[]
    expect(remaining).toHaveLength(1)
    expect(remaining[0]).toMatchObject({ id: 'legacy_p_known_stale_revoked', status: 'revoked' })
  })

  // Why (round 16): mirrors the coordinator case above for the worker/dispatch tier - 'settled' is
  // the worker analogue of 'revoked' (workers never reach 'revoked' in practice - see the
  // defensive-symmetry note above), since requireCommittedLegacyPrincipal and
  // legacy-lifecycle-operation.ts both already deny live legacy actions to a 'settled' principal.
  it('prefers a settled legacy worker principal over a committed one when the dispatch assignee_handle matches neither', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-dup-worker-known-stale-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    const run = seed.createRun({
      objective: 'dup worker known-stale repro',
      coordinatorHandle: 'term_coord',
      coordinatorPaneKey: 'tab_coord:88888888-8888-4888-9888-888888888888'
    })
    const task = seed.createTask({ spec: 'legacy worker known-stale repro', runId: run.id })
    const dispatch = seed.createDispatchContext(task.id, 'term_worker_modern', 'tab_worker:leaf')
    seed.close()
    const raw = new Database(dbPath)
    raw.exec('DROP INDEX idx_legacy_principal_dispatch')
    raw.exec(`
      DROP TABLE legacy_compatibility_principals;
      CREATE TABLE legacy_compatibility_principals (
        id                  TEXT PRIMARY KEY,
        run_id              TEXT NOT NULL,
        dispatch_id         TEXT,
        role                TEXT NOT NULL CHECK(role IN ('worker', 'coordinator')),
        host_scope          TEXT NOT NULL,
        terminal_handle     TEXT NOT NULL,
        pane_key            TEXT NOT NULL,
        launch_token_hash   TEXT NOT NULL,
        process_incarnation TEXT,
        status              TEXT NOT NULL CHECK(status IN ('committed', 'settled', 'revoked')),
        CHECK(
          (role = 'worker' AND dispatch_id IS NOT NULL) OR
          (role = 'coordinator' AND dispatch_id IS NULL)
        )
      );
    `)
    const insertPrincipal = raw.prepare(
      `INSERT INTO legacy_compatibility_principals (
         id, run_id, dispatch_id, role, host_scope, terminal_handle, pane_key,
         launch_token_hash, process_incarnation, status
       ) VALUES (?, ?, ?, 'worker', '{}', ?, ?, ?, NULL, ?)`
    )
    // Inserted FIRST (lower rowid) and settled - a plain highest-rowid tie-break would pick the
    // OTHER (committed) row instead, so this genuinely isolates the round-16 inversion. (Round
    // 14's own status-preference tier is a no-op either way here - neither row is 'revoked'.)
    insertPrincipal.run(
      'legacy_p_worker_known_stale_settled',
      run.id,
      dispatch.id,
      'term_worker_old_b',
      'tab_b:leaf',
      'hash_b',
      'settled'
    )
    insertPrincipal.run(
      'legacy_p_worker_known_stale_committed',
      run.id,
      dispatch.id,
      'term_worker_old_a',
      'tab_a:leaf',
      'hash_a',
      'committed'
    )
    raw.pragma(`user_version = ${SCHEMA_VERSION}`)
    raw.close()

    db = new OrchestrationDb(dbPath)

    const rawAfter = (db as unknown as { db: Database.Database }).db
    const remaining = rawAfter
      .prepare("SELECT id, status FROM legacy_compatibility_principals WHERE role = 'worker'")
      .all() as { id: string; status: string }[]
    expect(remaining).toHaveLength(1)
    expect(remaining[0]).toMatchObject({
      id: 'legacy_p_worker_known_stale_settled',
      status: 'settled'
    })
  })

  // Why (round 12): the v29 tasks CHECK-constraint probe is a distinct fact from the
  // terminal_reason/replacement_task_id columns the generic matrix already covers (both are
  // added in the SAME rebuild, but the resolver needs to check the CHECK independently - see
  // tasksAllowCancelledOrSuperseded's own docstring).
  it('rewinds a database claiming the current version whose tasks CHECK constraint predates v29', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-db-version-skew-v29-check-'))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    seed.close()
    const raw = new Database(dbPath)
    raw.exec(`
      CREATE TABLE tasks_pre_v29 (
        id            TEXT PRIMARY KEY,
        run_id        TEXT NOT NULL,
        parent_id     TEXT,
        created_by_terminal_handle TEXT,
        created_by_pane_key TEXT,
        created_by_process_incarnation TEXT,
        created_by_run_generation INTEGER,
        task_title    TEXT,
        display_name  TEXT,
        spec          TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending', 'ready', 'dispatched', 'completed', 'failed', 'blocked')),
        deps          TEXT NOT NULL DEFAULT '[]',
        result        TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at  TEXT,
        terminal_reason      TEXT,
        replacement_task_id  TEXT
      );
      INSERT INTO tasks_pre_v29 (
        id, run_id, parent_id, created_by_terminal_handle, created_by_pane_key,
        created_by_process_incarnation, created_by_run_generation,
        task_title, display_name, spec, status, deps, result, created_at, completed_at
      )
      SELECT
        id, run_id, parent_id, created_by_terminal_handle, created_by_pane_key,
        created_by_process_incarnation, created_by_run_generation,
        task_title, display_name, spec, status, deps, result, created_at, completed_at
      FROM tasks;
      DROP TABLE tasks;
      ALTER TABLE tasks_pre_v29 RENAME TO tasks;
      CREATE INDEX idx_tasks_status ON tasks(status);
      CREATE INDEX idx_tasks_parent ON tasks(parent_id);
      CREATE INDEX idx_tasks_run_status ON tasks(run_id, status);
    `)
    raw.pragma(`user_version = ${SCHEMA_VERSION}`)

    expect(resolveOrchestrationMigrationStartVersion(raw, SCHEMA_VERSION, SCHEMA_VERSION)).toBe(6)

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
        'idx_messages_unread_current_run_type',
        'idx_legacy_principal_coordinator',
        'idx_legacy_principal_dispatch'
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

  function seedAndStripBoundaryArtifacts(
    label: string,
    { columns, indexes, tables }: (typeof VERSION_BOUNDARY_DROPS)[number]
  ): Database.Database {
    tempDir = mkdtempSync(join(tmpdir(), `orca-db-version-skew-${label}-`))
    const dbPath = join(tempDir, 'orchestration.db')
    const seed = new OrchestrationDb(dbPath)
    seed.close()
    const raw = new Database(dbPath)
    // Why indexes/triggers before columns: anything whose definition references a column
    // blocks that column's DROP COLUMN until it's gone too - messages.delivery_contract is
    // also read by a trigger, not just indexes. Only drop a trigger whose own SQL references one
    // of THIS iteration's columns - a v26 test case (say) must not incidentally strip the v26
    // mutation-receipt-count triggers meant for a DIFFERENT test case's probe.
    for (const index of indexes ?? []) {
      raw.exec(`DROP INDEX ${index}`)
    }
    const droppedColumnNames = new Set((columns ?? []).map(([, column]) => column))
    for (const trigger of raw
      .prepare("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
      .all() as { name: string; sql: string }[]) {
      if ([...droppedColumnNames].some((column) => trigger.sql.includes(column))) {
        raw.exec(`DROP TRIGGER ${trigger.name}`)
      }
    }
    for (const [table, column] of columns ?? []) {
      raw.exec(`ALTER TABLE ${table} DROP COLUMN ${column}`)
    }
    for (const table of tables ?? []) {
      raw.exec(`DROP TABLE ${table}`)
    }
    return raw
  }

  it.each(VERSION_BOUNDARY_DROPS)(
    'does not rewind a healthy database exactly one version below the v$version boundary',
    (entry) => {
      const raw = seedAndStripBoundaryArtifacts(`pre-v${entry.version}`, entry)
      const storedVersion = entry.version - 1
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

  // Why (round 12, fix for a real methodology bug an independent review found): the matrix above
  // only tests the false-POSITIVE direction (a healthy old database must not be wrongly rewound)
  // - it can never fail if a VERSIONED_POST_V6_COLUMNS/INDEXES entry is deleted entirely, since
  // removing an entry can only make the completeness check MORE permissive, and every assertion
  // above is "must not rewind". Proven empirically: with round 11's whole source fix reverted,
  // all 23 cases above still passed. This mirror tests the actual regression class round 11 fixed
  // - a database FALSELY CLAIMING the current schema version while genuinely missing one of
  // these artifacts (exactly what a stale user_version pragma or a corrupted/hand-edited database
  // looks like) must be detected and rewound to 6, not trusted at face value.
  // Why honesty, not more coverage, is the fix here for now (round 13, from an independent
  // review's mutation-sweep finding, deliberately NOT fully resolved this round): each case
  // above groups every artifact a version introduces together and only asserts the WHOLE group
  // is detected - it does not prove any SPECIFIC entry's own gate fires, because
  // hasCompletePostV6Schema's `&&` chain short-circuits on the FIRST failing check, and several
  // versions (v19 especially, with 6 columns + 3 indexes in one case) bundle artifacts that
  // would mask each other if only one were actually gated. A mutation sweep (delete one
  // completeness-check entry at a time, run the suite) found the bulk of individual entries -
  // not just v19's - currently survive undetected this way, purely because a SIBLING entry in
  // the same group happens to catch the same corrupted-database state first. Flattening this
  // into one case per artifact (stripping exactly what SQLite forces alongside it, nothing more)
  // would close that gap but is a substantially larger rewrite than this round's fix scope -
  // deliberately deferred, not silently accepted. What IS still proven here: every group as a
  // WHOLE is detected (a version's migration block genuinely not having run is caught), which is
  // the realistic corruption shape; what is NOT proven is that every listed entry is individually
  // load-bearing versus redundant with a sibling.
  it.each(VERSION_BOUNDARY_DROPS)(
    'rewinds a database claiming the current version but missing its v$version artifact',
    (entry) => {
      const raw = seedAndStripBoundaryArtifacts(`claims-current-missing-v${entry.version}`, entry)
      raw.pragma(`user_version = ${SCHEMA_VERSION}`)

      expect(resolveOrchestrationMigrationStartVersion(raw, SCHEMA_VERSION, SCHEMA_VERSION)).toBe(6)

      raw.close()
    }
  )
})
