import { LEGACY_RUN_ID } from '../contract-constants'
import type { OrchestrationDb } from '../orchestration-db'

// v28 → v29: SQLite can't ALTER a CHECK, so rebuild tasks to allow 'cancelled'/'superseded' and
// carry terminal_reason/replacement_task_id (#14548). Existing rows keep their status untouched —
// this only widens what the column accepts, it never reclassifies historical 'failed' Tasks.
export function migrateTaskTerminalStates(this: OrchestrationDb): void {
  if (this.tasksStatusCheckAllowsCancelled()) {
    return
  }
  // Why: recreate indexes here — DROP TABLE drops them; createTables re-runs only next startup.
  this.db.exec(`
      CREATE TABLE tasks_new (
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
          CHECK(status IN (
            'pending', 'ready', 'dispatched', 'completed', 'failed', 'blocked',
            'cancelled', 'superseded'
          )),
        deps          TEXT NOT NULL DEFAULT '[]',
        result        TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at  TEXT,
        terminal_reason      TEXT,
        replacement_task_id  TEXT
      );
      INSERT INTO tasks_new (
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
      ALTER TABLE tasks_new RENAME TO tasks;

      CREATE INDEX idx_tasks_status ON tasks(status);
      CREATE INDEX idx_tasks_parent ON tasks(parent_id);
      CREATE INDEX idx_tasks_run_status ON tasks(run_id, status);
    `)
}
