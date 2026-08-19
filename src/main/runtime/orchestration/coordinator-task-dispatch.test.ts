import { afterEach, describe, expect, it } from 'vitest'
import { warnStaleDispatches } from './coordinator-task-dispatch'
import { OrchestrationDb } from './db'

// Why (#14829/#10673): escalateStaleDispatch used to stamp stale_escalated_at before confirming a
// target existed, so a dispatch with no resolvable mailbox at that instant was permanently
// unescalated — nothing short of a heartbeat ever un-stamps it.
describe('warnStaleDispatches escalation timing', () => {
  let db: OrchestrationDb

  afterEach(() => {
    db?.close()
  })

  function makeStaleDispatch(): string {
    const task = db.createTask({ spec: 'work' })
    const ctx = db.createDispatchContext(task.id, 'term_stale')
    const sqlite = (
      db as unknown as { db: { prepare: (s: string) => { run: (...a: unknown[]) => void } } }
    ).db
    const staleIso = new Date(Date.now() - 60 * 60 * 1000).toISOString()
    sqlite
      .prepare('UPDATE dispatch_contexts SET dispatched_at = ? WHERE id = ?')
      .run(staleIso, ctx.id)
    return ctx.id
  }

  function escalationCount(): number {
    return (
      db as unknown as {
        db: { prepare: (s: string) => { get: () => { count: number } } }
      }
    ).db
      .prepare("SELECT COUNT(*) AS count FROM messages WHERE type = 'escalation'")
      .get().count
  }

  it('does not stamp stale_escalated_at when no escalation target resolves (legacy Run, no active coordinator)', () => {
    db = new OrchestrationDb(':memory:')
    const dispatchId = makeStaleDispatch()

    warnStaleDispatches(db, () => {})

    expect(db.getDispatchContextById(dispatchId)?.stale_escalated_at).toBeFalsy()
    expect(escalationCount()).toBe(0)
  })

  it('self-heals: a later sweep escalates once a target becomes resolvable, since the earlier miss left the row unstamped', () => {
    db = new OrchestrationDb(':memory:')
    const dispatchId = makeStaleDispatch()

    warnStaleDispatches(db, () => {})
    expect(db.getDispatchContextById(dispatchId)?.stale_escalated_at).toBeFalsy()

    db.createCoordinatorRun({ spec: 'go', coordinatorHandle: 'coord' })
    warnStaleDispatches(db, () => {})

    expect(db.getDispatchContextById(dispatchId)?.stale_escalated_at).toBeTruthy()
    expect(escalationCount()).toBe(1)
  })
})
