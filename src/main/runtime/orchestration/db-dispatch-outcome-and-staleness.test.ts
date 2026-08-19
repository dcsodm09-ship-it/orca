import { afterEach, describe, expect, it } from 'vitest'
import { OrchestrationDb } from './db'

// Why: split out of db.test.ts to keep that file under the max-lines budget.
describe('OrchestrationDb dispatch outcome and staleness', () => {
  let db: OrchestrationDb | undefined

  afterEach(() => {
    db?.close()
  })

  function createDb(): OrchestrationDb {
    db = new OrchestrationDb(':memory:')
    return db
  }

  // #8984: a Dispatch that fails on exit reverts its Task to 'ready' with no
  // active assignee — the last Dispatch's outcome must stay visible there.
  it('listTasksWithDispatch surfaces a dead Dispatch outcome once no assignee remains', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_worker')
    d.failDispatch(ctx.id, 'Agent exited with code -1', { workerProcessExited: true })

    const rows = d.listTasksWithDispatch()
    const row = rows.find((r) => r.id === task.id)
    expect(row?.assignee_handle).toBeNull()
    expect(row?.dispatch_id).toBeNull()
    expect(row?.last_dispatch_status).toBe('failed')
    expect(row?.last_dispatch_failure).toBe('Agent exited with code -1')
  })

  it('markDispatchStaleEscalated stamps stale_escalated_at (#14829)', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_a')
    expect(d.getDispatchContext(task.id)?.stale_escalated_at).toBeNull()

    d.markDispatchStaleEscalated(ctx.id, '2026-05-04T00:00:00.000Z')
    expect(d.getDispatchContext(task.id)?.stale_escalated_at).toBe('2026-05-04T00:00:00.000Z')
  })

  it('recordHeartbeat clears a prior stale_escalated_at so a relapse can re-escalate (#14829)', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_a')
    d.markDispatchStaleEscalated(ctx.id, '2026-05-04T00:00:00.000Z')

    d.recordHeartbeat(ctx.id, '2026-05-04T00:10:00.000Z')
    expect(d.getDispatchContext(task.id)?.stale_escalated_at).toBeNull()
  })
})
