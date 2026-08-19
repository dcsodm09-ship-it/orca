import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import Database from '../../sqlite/sync-database'
import { LEGACY_RUN_ID, OrchestrationDb } from './db'

describe('task terminal states migration (#14548)', () => {
  let db: OrchestrationDb | undefined
  let tempDir: string | undefined

  afterEach(() => {
    db?.close()
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('rebuilds a v28 tasks table to accept cancelled/superseded without losing rows', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-task-terminal-migration-'))
    const dbPath = join(tempDir, 'orchestration.db')
    db = new OrchestrationDb(dbPath)
    db.close()
    db = undefined

    // Why: SQLite can't drop a CHECK — rebuild `tasks` back to its pre-#14548 (v28) shape.
    const oldDb = new Database(dbPath)
    oldDb.exec(`
      CREATE TABLE tasks_v28 (
        id            TEXT PRIMARY KEY,
        run_id        TEXT NOT NULL DEFAULT '${LEGACY_RUN_ID}',
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
        completed_at  TEXT
      );
      INSERT INTO tasks_v28 (
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
      ALTER TABLE tasks_v28 RENAME TO tasks;
      CREATE INDEX idx_tasks_status ON tasks(status);
      CREATE INDEX idx_tasks_parent ON tasks(parent_id);
      CREATE INDEX idx_tasks_run_status ON tasks(run_id, status);
    `)
    oldDb
      .prepare(
        `INSERT INTO tasks (id, run_id, spec, status) VALUES ('task_legacy_failed', ?, 'legacy work', 'failed')`
      )
      .run(LEGACY_RUN_ID)
    oldDb.pragma('user_version = 28')
    oldDb.close()

    db = new OrchestrationDb(dbPath)
    const sqlite = (db as unknown as { db: Database.Database }).db

    expect(sqlite.pragma('user_version', { simple: true })).toBe(31)
    // Why: migration must never reclassify a historical failed Task (#14548 required semantics).
    expect(db.getTask('task_legacy_failed')).toMatchObject({
      status: 'failed',
      terminal_reason: null,
      replacement_task_id: null
    })
    // Why: DROP TABLE tasks drops idx_tasks_run_status too — losing it trips the post-v6
    // version-skew detector into rewinding to v6 and re-running the whole migration chain
    // (including adoptLegacyRunIfNeeded) on every future launch of this now-healthy db.
    expect(
      sqlite
        .prepare(
          "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_tasks_run_status'"
        )
        .get()
    ).toBeTruthy()

    const task = db.createTask({ spec: 'post-migration work' })
    db.cancelTask(task.id, 'cancelled', { reason: 'no longer needed' })
    expect(db.getTask(task.id)).toMatchObject({
      status: 'cancelled',
      terminal_reason: 'no longer needed'
    })

    // Why: a healthy post-v29 db must not rewind and re-run the v7-v28 migration chain
    // (adoptLegacyRunIfNeeded etc.) on a later launch just because it was already migrated once.
    db.close()
    db = new OrchestrationDb(dbPath)
    const sqliteReopened = (db as unknown as { db: Database.Database }).db
    expect(sqliteReopened.pragma('user_version', { simple: true })).toBe(31)
    expect(db.getTask('task_legacy_failed')?.status).toBe('failed')
    expect(db.getTask(task.id)?.status).toBe('cancelled')
  })

  it('is a no-op once the tasks CHECK already allows cancelled', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-task-terminal-migration-noop-'))
    const dbPath = join(tempDir, 'orchestration.db')
    db = new OrchestrationDb(dbPath)
    const task = db.createTask({ spec: 'fresh db work' })

    db.close()
    db = new OrchestrationDb(dbPath)
    const sqlite = (db as unknown as { db: Database.Database }).db
    expect(sqlite.pragma('user_version', { simple: true })).toBe(31)
    expect(db.getTask(task.id)?.status).toBe('ready')
  })
})
