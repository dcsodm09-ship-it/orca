import {
  LEGACY_RUN_ID,
  LEGACY_CONTRACT_VERSION,
  CURRENT_CONTRACT_VERSION
} from '../contract-constants'
import { hasLifecycleRejectionMarker } from '../lifecycle-rejection-marker'
import type { OrchestrationDb } from '../orchestration-db'

export function migrateLegacyContractStorage(this: OrchestrationDb): void {
  if (!this.hasColumn('dispatch_contexts', 'contract_version')) {
    this.db.exec(
      `ALTER TABLE dispatch_contexts
       ADD COLUMN contract_version INTEGER NOT NULL DEFAULT ${CURRENT_CONTRACT_VERSION}`
    )
  }
  if (!this.hasColumn('dispatch_contexts', 'launch_token_hash')) {
    this.db.exec('ALTER TABLE dispatch_contexts ADD COLUMN launch_token_hash TEXT')
  }
  if (!this.hasColumn('messages', 'delivery_contract')) {
    this.db.exec(
      `ALTER TABLE messages
       ADD COLUMN delivery_contract TEXT NOT NULL DEFAULT 'current_delivery'
       CHECK(delivery_contract IN ('legacy_direct', 'current_delivery', 'audit_only'))`
    )
  }
  this.db.exec(`
    CREATE INDEX IF NOT EXISTS idx_messages_delivery_contract
      ON messages(run_id, delivery_contract, to_handle, read, sequence);

    CREATE TABLE IF NOT EXISTS legacy_adoptions (
      source_run_id        TEXT PRIMARY KEY,
      adopted_run_id       TEXT UNIQUE NOT NULL,
      scheduler_state_lost INTEGER NOT NULL,
      adopted_at           TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS legacy_compatibility_principals (
      id                  TEXT PRIMARY KEY,
      run_id              TEXT NOT NULL,
      dispatch_id         TEXT,
      role                TEXT NOT NULL CHECK(role IN ('worker', 'coordinator')),
      host_scope          TEXT NOT NULL,
      terminal_handle     TEXT NOT NULL,
      pane_key            TEXT NOT NULL,
      launch_token_hash   TEXT NOT NULL,
      process_incarnation TEXT,
      status              TEXT NOT NULL
        CHECK(status IN ('committed', 'settled', 'revoked')),
      CHECK(
        (role = 'worker' AND dispatch_id IS NOT NULL) OR
        (role = 'coordinator' AND dispatch_id IS NULL)
      ),
      UNIQUE(role, run_id, dispatch_id)
    );

    -- Why (round 13, fix for a real regression an independent review found): a database with
    -- duplicate coordinator/worker principals (only reachable via external corruption/repair
    -- tooling, never via normal writes - commitLegacyCompatibilityPrincipal's own SELECT-then-
    -- INSERT is serialized under BEGIN IMMEDIATE, and its "existing" lookup has no status filter
    -- so it throws rather than inserting a second row the moment ANY row - committed, settled, or
    -- revoked - already exists for that run_id/dispatch_id; verified empirically in round 15) used
    -- to boot in a silently-degraded state (getLegacyCoordinatorPrincipal /
    -- commitLegacyCompatibilityPrincipal's own "existing" lookup can each pick a different one of
    -- several arbitrarily). Once this migration's own idempotent re-run started being trusted as a
    -- completeness signal (the schema-version-skew probe added in round 12), that database instead
    -- hit this exact CREATE UNIQUE INDEX on its own leftover duplicates and threw, permanently: a
    -- completeness check that diagnoses a repairable state correctly must not also make it
    -- unrepairable.
    -- Why round 15 added the 2 DELETEs directly below, running BEFORE everything else (fix for a
    -- real design gap an independent review found in round 14's own fix): round 14 made the
    -- MAX(rowid) tie-break prefer any non-revoked row over a revoked one, reasoning that 'revoked'
    -- is unrepairable. But 'revoked' is not a corruption marker - it is the coordinator's genuine
    -- terminal state after a legitimate takeover (the only site that ever writes it,
    -- run-binding.ts's bindRun, revokes the OUTGOING coordinator's principal in place while leaving
    -- it in the table). Since duplicates can only exist via external corruption in the first place
    -- (see the paragraph above), status alone - revoked or not - has no guaranteed correlation with
    -- which duplicate is still the one the run actually recognizes: a corrupted DB could just as
    -- easily retain a stale 'committed' row alongside the genuinely-current row now correctly
    -- 'revoked'. The one signal that IS authoritative regardless of how the duplicate arose is
    -- which row's terminal_handle matches the run's live "coordinator_handle" (or, for a worker,
    -- the dispatch's live "assignee_handle") - prefer that row first, unconditionally on its own
    -- status, and only fall through to the older revoked/rowid heuristics below for whatever a
    -- group this doesn't resolve (e.g. no duplicate's terminal_handle matches the live binding at
    -- all, or the binding itself is gone).
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'coordinator'
        AND run_id IN (
          SELECT lcp.run_id FROM legacy_compatibility_principals lcp
          JOIN runs r ON r.id = lcp.run_id
          WHERE lcp.role = 'coordinator' AND lcp.terminal_handle = r.coordinator_handle
        )
        AND NOT EXISTS (
          SELECT 1 FROM runs r
          WHERE r.id = legacy_compatibility_principals.run_id
            AND r.coordinator_handle = legacy_compatibility_principals.terminal_handle
        );
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'worker'
        AND dispatch_id IN (
          SELECT lcp.dispatch_id FROM legacy_compatibility_principals lcp
          JOIN dispatch_contexts dc ON dc.id = lcp.dispatch_id
          WHERE lcp.role = 'worker' AND lcp.terminal_handle = dc.assignee_handle
        )
        AND NOT EXISTS (
          SELECT 1 FROM dispatch_contexts dc
          WHERE dc.id = legacy_compatibility_principals.dispatch_id
            AND dc.assignee_handle = legacy_compatibility_principals.terminal_handle
        );
    -- Why round 14 added the 2 DELETEs directly below (fix for a real bug an independent review
    -- found and reproduced): a plain MAX(rowid) tie-break could keep a 'revoked' duplicate over a
    -- 'committed'/'settled' one. This is now only the fallback tier for whatever the
    -- authoritative-match step above didn't resolve, so prefer any non-revoked row over a revoked
    -- one first within what's left; only once THAT remainder is either all-non-revoked or
    -- all-revoked does the MAX(rowid) tie-break below decide among it - there is still no
    -- timestamp column to do better than that.
    -- Why "status-homogeneous" is not claimed here (round 15 accuracy note, an independent review
    -- found the prior wording overstated this): a remainder can still mix 'committed' and
    -- 'settled' (both non-revoked) - the guarantee is only that no revoked row survives alongside
    -- a non-revoked sibling, not that every survivor of this step shares one status.
    -- Why "highest rowid", not "most recently inserted" (round 14 accuracy note, an independent
    -- review found this claim overstated): SQLite does not guarantee implicit rowids track
    -- insertion order across VACUUM or explicit-rowid repair tooling - MAX(rowid) is still a
    -- fully deterministic tie-break (the same row wins every time this migration re-runs), just
    -- not provably "the newest one" in every conceivable corrupted-database scenario.
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'coordinator' AND status = 'revoked'
        AND EXISTS (
          SELECT 1 FROM legacy_compatibility_principals other
          WHERE other.role = 'coordinator'
            AND other.run_id = legacy_compatibility_principals.run_id
            AND other.status != 'revoked'
        );
    -- Why "worker AND status = 'revoked'" below is defensive rather than a live path (round 15
    -- accuracy note, an independent review found this): setLegacyCompatibilityPrincipalStatus is
    -- only ever called with 'revoked' for a coordinator principal (run-binding.ts:133) - a worker
    -- row reaches 'revoked' only via external corruption, never via normal writes. Kept for
    -- defensive symmetry with the coordinator case above, not because it is currently reachable.
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'worker' AND status = 'revoked'
        AND EXISTS (
          SELECT 1 FROM legacy_compatibility_principals other
          WHERE other.role = 'worker'
            AND other.dispatch_id IS legacy_compatibility_principals.dispatch_id
            AND other.status != 'revoked'
        );
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'coordinator'
        AND rowid NOT IN (
          SELECT MAX(rowid) FROM legacy_compatibility_principals
          WHERE role = 'coordinator'
          GROUP BY run_id
        );
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'worker'
        AND rowid NOT IN (
          SELECT MAX(rowid) FROM legacy_compatibility_principals
          WHERE role = 'worker'
          GROUP BY dispatch_id
        );

    -- Why these 2 indexes are safe to create here, unlike most of this file's other artifacts
    -- (round 14 accuracy note, an independent review found this dependency undocumented): they
    -- exist ONLY via this migration, never via createTables()'s current-schema CREATE ... IF NOT
    -- EXISTS pass (which runs before migrate() and would otherwise throw on the very duplicates
    -- the DELETEs above exist to clear, bypassing the repair entirely) - if either index is ever
    -- added to createTables() too, the DELETEs above must move there as well, or be run first.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_principal_coordinator
      ON legacy_compatibility_principals(run_id)
      WHERE role = 'coordinator';
    CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_principal_dispatch
      ON legacy_compatibility_principals(dispatch_id)
      WHERE role = 'worker';

    CREATE TABLE IF NOT EXISTS legacy_operation_receipts (
      principal_id   TEXT NOT NULL,
      operation_key  TEXT NOT NULL,
      method         TEXT NOT NULL,
      payload_hash   TEXT NOT NULL,
      effect_id      TEXT NOT NULL,
      response_json  TEXT NOT NULL,
      completed_at   TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY(principal_id, operation_key)
    );

    CREATE TABLE IF NOT EXISTS legacy_mail_receipts (
      principal_id    TEXT NOT NULL,
      message_id      TEXT NOT NULL,
      acknowledged_at TEXT,
      PRIMARY KEY(principal_id, message_id)
    );
  `)

  this.db
    .prepare(
      `UPDATE dispatch_contexts
       SET contract_version = ?
       WHERE run_id = ? AND capability_hash IS NULL`
    )
    .run(LEGACY_CONTRACT_VERSION, LEGACY_RUN_ID)
  this.classifyLegacyMessageContracts(LEGACY_RUN_ID, false)
  this.ensureLegacySchedulerLossColumn()
  this.adoptLegacyRunIfNeeded()
}

export function classifyLegacyMessageContracts(
  this: OrchestrationDb,
  runId: string,
  adoptedOnly: boolean
): void {
  const contractFilter = adoptedOnly
    ? " AND delivery_contract IN ('legacy_direct', 'audit_only')"
    : ''
  this.db
    .prepare(
      `UPDATE messages SET delivery_contract = 'legacy_direct'
       WHERE run_id = ?${contractFilter}`
    )
    .run(runId)
  const rows = this.db
    .prepare(`SELECT id, payload FROM messages WHERE run_id = ?${contractFilter}`)
    .all(runId) as { id: string; payload: string | null }[]
  const markAuditOnly = this.db.prepare(
    "UPDATE messages SET delivery_contract = 'audit_only' WHERE id = ? AND run_id = ?"
  )
  for (const row of rows) {
    if (hasLifecycleRejectionMarker(row.payload)) {
      markAuditOnly.run(row.id, runId)
    }
  }
}

export function migrateLegacySchedulerLossProvenance(this: OrchestrationDb): void {
  this.ensureLegacySchedulerLossColumn()
  this.adoptLegacyRunIfNeeded()
  const adoption = this.getLegacyAdoption()
  if (adoption) {
    this.classifyLegacyMessageContracts(adoption.adopted_run_id, true)
  }
}

export function ensureLegacySchedulerLossColumn(this: OrchestrationDb): void {
  if (!this.hasColumn('coordinator_runs', 'scheduler_lost_at')) {
    this.db.exec('ALTER TABLE coordinator_runs ADD COLUMN scheduler_lost_at TEXT')
  }
}

export type MigrateLegacyContractStorageMethods = {
  migrateLegacyContractStorage: typeof migrateLegacyContractStorage
  classifyLegacyMessageContracts: typeof classifyLegacyMessageContracts
  migrateLegacySchedulerLossProvenance: typeof migrateLegacySchedulerLossProvenance
  ensureLegacySchedulerLossColumn: typeof ensureLegacySchedulerLossColumn
}

export function attachMigrateLegacyContractStorage(ctor: { prototype: object }): void {
  Object.assign(ctor.prototype, {
    migrateLegacyContractStorage,
    classifyLegacyMessageContracts,
    migrateLegacySchedulerLossProvenance,
    ensureLegacySchedulerLossColumn
  })
}
