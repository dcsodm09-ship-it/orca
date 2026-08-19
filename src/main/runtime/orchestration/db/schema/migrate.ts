import { resolveOrchestrationMigrationStartVersion } from '../../orchestration-schema-version-skew'
import { SCHEMA_VERSION } from '../contract-constants'
import type { OrchestrationDb } from '../orchestration-db'
import { applySchemaMigrationsV13ToV28 } from './migrate-v13-v28'
import { applySchemaMigrationsV2ToV12 } from './migrate-v2-v12'
import { migrateTaskTerminalStates } from './migrate-task-terminal-states'

// Why: CREATE TABLE IF NOT EXISTS won't alter existing DBs; migrate in a txn that bumps user_version only on success (atomic all-or-nothing).
export function migrate(this: OrchestrationDb): void {
  const storedVersion = this.db.pragma('user_version', { simple: true }) as number
  const current = resolveOrchestrationMigrationStartVersion(this.db, storedVersion, SCHEMA_VERSION)
  if (current >= SCHEMA_VERSION) {
    return
  }

  this.db.exec('BEGIN IMMEDIATE')
  try {
    applySchemaMigrationsV2ToV12.call(this, current)
    applySchemaMigrationsV13ToV28.call(this, current)
    if (current < 29) {
      migrateTaskTerminalStates.call(this)
    }
    // v29 → v30: exit-provenance for #15048 — nullable, defaults NULL (unknown/crash).
    if (current < 30 && !this.hasColumn('worker_dispatches', 'terminated_by')) {
      this.db.exec('ALTER TABLE worker_dispatches ADD COLUMN terminated_by TEXT')
    }
    // v30 → v31: #14829 — nullable episode marker so warnStaleDispatches escalates once per stale spell, not every tick.
    if (current < 31 && !this.hasColumn('dispatch_contexts', 'stale_escalated_at')) {
      this.db.exec('ALTER TABLE dispatch_contexts ADD COLUMN stale_escalated_at TEXT')
    }
    this.db.pragma(`user_version = ${SCHEMA_VERSION}`)
    this.db.exec('COMMIT')
  } catch (err) {
    this.db.exec('ROLLBACK')
    throw err
  }
}

export type SchemaMigrateMethods = {
  migrate: typeof migrate
}

export function attachSchemaMigrate(ctor: { prototype: object }): void {
  Object.assign(ctor.prototype, {
    migrate
  })
}
