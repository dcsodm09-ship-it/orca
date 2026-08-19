import type { OrchestrationDb } from '../orchestration-db'

// Why (round 13, fix for a real regression an independent review found): a database with
// duplicate coordinator/worker principals (only reachable via external corruption/repair
// tooling, never via normal writes - commitLegacyCompatibilityPrincipal's own SELECT-then-
// INSERT is serialized under BEGIN IMMEDIATE, and its "existing" lookup has no status filter
// so it throws rather than inserting a second row the moment ANY row - committed, settled, or
// revoked - already exists for that run_id/dispatch_id; verified empirically in round 15) used
// to boot in a silently-degraded state (getLegacyCoordinatorPrincipal /
// commitLegacyCompatibilityPrincipal's own "existing" lookup can each pick a different one of
// several arbitrarily). Once migrate-legacy-contract-storage.ts's own idempotent re-run started
// being trusted as a completeness signal (the schema-version-skew probe added in round 12), that
// database instead hit this exact CREATE UNIQUE INDEX on its own leftover duplicates and threw,
// permanently: a completeness check that diagnoses a repairable state correctly must not also
// make it unrepairable.
// Why round 15 added the 2 DELETEs directly below, running BEFORE everything else (fix for a
// real design gap an independent review found in round 14's own fix): round 14 made the
// MAX(rowid) tie-break prefer any non-revoked row over a revoked one, reasoning that 'revoked'
// is unrepairable. But 'revoked' is not a corruption marker - it is the coordinator's genuine
// terminal state after a legitimate takeover (the only site that ever writes it,
// run-binding.ts's bindRun, revokes the OUTGOING coordinator's principal in place while leaving
// it in the table). Since duplicates can only exist via external corruption in the first place
// (see the paragraph above), status alone - revoked or not - has no guaranteed correlation with
// which duplicate is still the one the run actually recognizes: a corrupted DB could just as
// easily retain a stale 'committed' row alongside the genuinely-current row now correctly
// 'revoked'. The one signal that IS authoritative regardless of how the duplicate arose is
// which row's terminal_handle matches the run's live "coordinator_handle" (or, for a worker,
// the dispatch's live "assignee_handle") - prefer that row first, unconditionally on its own
// status, and only fall through to the later tiers below for whatever a group this doesn't
// resolve: no duplicate's terminal_handle matches the live binding at all (round 16 handles
// the sub-case where a live binding still exists, just matching none of them; the remaining
// sub-case - no live binding at all - falls all the way through to round 14's tier), the
// binding itself is gone, or multiple duplicates all match the live handle (this tier keeps
// all of them, deferring to the later status/rowid tiers to reduce that group to one).
// Why this step depends on the "runs"/"dispatch_contexts" tables already having their
// "coordinator_handle"/"assignee_handle" columns (round 16 accuracy note, an independent
// review flagged this undocumented dependency): both are core v2-baseline columns created by
// createTables() well before migrate() ever runs, unlike the columns/tables this file's OTHER
// artifacts depend on - so this is safe on every schema version this migration can run
// against, but worth naming explicitly since a wrong assumption here would throw mid-migrate()
// and roll back the whole transaction, the same failure class rounds 12-14 fixed elsewhere.
// Why round 16 added the 2 DELETEs after those (fix for a real P2 an independent review
// found in round 15's own fix): the authoritative-match step above is a no-op in the MOST
// common post-takeover shape - a run's live coordinator_handle (or a dispatch's live
// assignee_handle) after a genuine handoff belongs to the NEW coordinator/worker, which never
// appears as a legacy principal's terminal_handle at all, so no duplicate ever matches it.
// When a run/dispatch DOES have a live handle but NO surviving duplicate's terminal_handle
// matches it, every remaining duplicate is definitively stale - none of them is the current
// one. In exactly that narrower case, invert round 14's preference below: prefer a
// 'revoked'/'settled' survivor over a 'committed' one, because a 'committed' row here would
// let a stale legacy identity still authenticate via commitLegacyCompatibilityPrincipal's
// existing-row match and exert live legacy operations (ask/heartbeat/worker_done) against a
// run/dispatch it no longer has authority over, while 'revoked' (coordinator) or 'settled'
// (worker, which never reaches 'revoked' - see below) both correctly block that via
// requireCommittedLegacyPrincipal / the existing.status==='revoked' check. Scoped tightly to a
// non-NULL live handle specifically because NULL (the default for a freshly adopted legacy
// run, or a dispatch with no assignee yet) carries no such certainty - there this tier is a
// no-op and the un-inverted tier below still applies.
// Why round 14's original 2 DELETEs still follow (fix for a real bug an independent review
// found and reproduced): a plain MAX(rowid) tie-break could keep a 'revoked' duplicate over a
// 'committed'/'settled' one. This is now only the fallback tier for whatever the
// authoritative-match and known-stale steps above didn't resolve (no live handle at all, or a
// group that is already status-uniform), so prefer any non-revoked row over a revoked one
// first within what's left; only once THAT remainder is either all-non-revoked or all-revoked
// does the MAX(rowid) tie-break below decide among it - there is still no timestamp column to
// do better than that.
// Why "status-homogeneous" is not claimed here (round 15 accuracy note, an independent review
// found the prior wording overstated this): a remainder can still mix 'committed' and
// 'settled' (both non-revoked) - the guarantee is only that no revoked row survives alongside
// a non-revoked sibling, not that every survivor of this step shares one status.
// Why "highest rowid", not "most recently inserted" (round 14 accuracy note, an independent
// review found this claim overstated): SQLite does not guarantee implicit rowids track
// insertion order across VACUUM or explicit-rowid repair tooling - MAX(rowid) is still a
// fully deterministic tie-break (the same row wins every time this migration re-runs), just
// not provably "the newest one" in every conceivable corrupted-database scenario.
// Why "worker AND status = 'revoked'" below is defensive rather than a live path (round 15
// accuracy note, an independent review found this): setLegacyCompatibilityPrincipalStatus is
// only ever called with 'revoked' for a coordinator principal (run-binding.ts:133) - a worker
// row reaches 'revoked' only via external corruption, never via normal writes. Kept for
// defensive symmetry with the coordinator case above, not because it is currently reachable.
// Why the 2 indexes at the end are safe to create here, unlike most of this file's other
// artifacts (round 14 accuracy note, an independent review found this dependency undocumented):
// they exist ONLY via this migration, never via createTables()'s current-schema
// CREATE ... IF NOT EXISTS pass (which runs before migrate() and would otherwise throw on the
// very duplicates the DELETEs above exist to clear, bypassing the repair entirely) - if either
// index is ever added to createTables() too, the DELETEs above must move there as well, or be
// run first.
export function dedupeLegacyCompatibilityPrincipals(this: OrchestrationDb): void {
  this.db.exec(`
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
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'coordinator' AND status != 'revoked'
        AND EXISTS (
          SELECT 1 FROM runs r
          WHERE r.id = legacy_compatibility_principals.run_id AND r.coordinator_handle IS NOT NULL
        )
        AND NOT EXISTS (
          SELECT 1 FROM runs r
          WHERE r.id = legacy_compatibility_principals.run_id
            AND r.coordinator_handle = legacy_compatibility_principals.terminal_handle
        )
        AND EXISTS (
          SELECT 1 FROM legacy_compatibility_principals other
          WHERE other.role = 'coordinator'
            AND other.run_id = legacy_compatibility_principals.run_id
            AND other.status = 'revoked'
        );
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'worker' AND status = 'committed'
        AND EXISTS (
          SELECT 1 FROM dispatch_contexts dc
          WHERE dc.id = legacy_compatibility_principals.dispatch_id AND dc.assignee_handle IS NOT NULL
        )
        AND NOT EXISTS (
          SELECT 1 FROM dispatch_contexts dc
          WHERE dc.id = legacy_compatibility_principals.dispatch_id
            AND dc.assignee_handle = legacy_compatibility_principals.terminal_handle
        )
        AND EXISTS (
          SELECT 1 FROM legacy_compatibility_principals other
          WHERE other.role = 'worker'
            AND other.dispatch_id IS legacy_compatibility_principals.dispatch_id
            AND other.status = 'settled'
        );
    DELETE FROM legacy_compatibility_principals
      WHERE role = 'coordinator' AND status = 'revoked'
        AND EXISTS (
          SELECT 1 FROM legacy_compatibility_principals other
          WHERE other.role = 'coordinator'
            AND other.run_id = legacy_compatibility_principals.run_id
            AND other.status != 'revoked'
        );
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

    CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_principal_coordinator
      ON legacy_compatibility_principals(run_id)
      WHERE role = 'coordinator';
    CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_principal_dispatch
      ON legacy_compatibility_principals(dispatch_id)
      WHERE role = 'worker';
  `)
}

export type DedupeLegacyCompatibilityPrincipalsMethods = {
  dedupeLegacyCompatibilityPrincipals: typeof dedupeLegacyCompatibilityPrincipals
}

export function attachDedupeLegacyCompatibilityPrincipals(ctor: { prototype: object }): void {
  Object.assign(ctor.prototype, { dedupeLegacyCompatibilityPrincipals })
}
