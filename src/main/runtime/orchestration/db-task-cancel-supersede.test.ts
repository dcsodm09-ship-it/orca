import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import type Database from '../../sqlite/sync-database'
import { OrchestrationDb } from './db'

type DatabaseHarness = {
  db: OrchestrationDb
  dir: string
  path: string
}

const harnesses: DatabaseHarness[] = []

afterEach(() => {
  const closed = harnesses.splice(0)
  for (const harness of closed) {
    harness.db.close()
  }
  for (const dir of new Set(closed.map((harness) => harness.dir))) {
    rmSync(dir, { recursive: true, force: true })
  }
})

describe('Task cancel/supersede (#14548 Phase 1)', () => {
  it.each(['cancelled', 'superseded'] as const)(
    'moves an undispatched Task to %s with a reason',
    (status) => {
      const { db } = createDatabase()
      const task = db.createTask({ spec: 'idle work' })

      const result = db.cancelTask(task.id, status, { reason: 'no longer needed' })

      expect(result).toMatchObject({
        status,
        terminal_reason: 'no longer needed',
        completed_at: expect.any(String)
      })
      expect(db.getTask(task.id)).toMatchObject({ status, terminal_reason: 'no longer needed' })
    }
  )

  it('atomically fences an active context-only Dispatch when superseding', () => {
    const { db } = createDatabase()
    const task = db.createTask({ spec: 'context-only work' })
    const dispatch = db.createDispatchContext(task.id, 'term_worker')

    const replacement = db.createTask({ spec: 'replacement work' })
    db.cancelTask(task.id, 'superseded', { replacementTaskId: replacement.id })

    expect(db.getTask(task.id)).toMatchObject({
      status: 'superseded',
      replacement_task_id: replacement.id
    })
    // Why: a fenced dispatch never finished — 'completed' would be a false success signal (#14548).
    expect(db.getDispatchContextById(dispatch.id)).toMatchObject({
      status: 'failed',
      last_failure: expect.stringContaining('superseded'),
      completed_at: expect.any(String),
      capability_revoked_at: expect.any(String)
    })
    expect(db.getActiveDispatchForTerminal('term_worker')).toBeUndefined()
  })

  it('rejects replacementTaskId from a different run', () => {
    const { db } = createDatabase()
    const otherRun = db.createRun({
      objective: 'other run',
      coordinatorHandle: 'term_other',
      coordinatorPaneKey: 'tab_other:ffffffff-ffff-4fff-8fff-ffffffffffff'
    })
    const task = db.createTask({ spec: 'run-scoped work' })
    const foreignReplacement = db.createTask({ spec: 'other run work', runId: otherRun.id })

    expect(() =>
      db.cancelTask(task.id, 'superseded', { replacementTaskId: foreignReplacement.id })
    ).toThrow(`must belong to run ${task.run_id}`)
    expect(db.getTask(task.id)?.status).toBe('ready')
  })

  it('rejects cancel while a supervised worker remains active', () => {
    const { db } = createDatabase()
    const task = db.createTask({ spec: 'supervised lifecycle' })
    const started = db.createStartingWorkerDispatch({ taskId: task.id, startOptions: {} })
    db.prepareStartingWorkerAuthority({
      dispatchId: started.dispatch.id,
      handle: 'term_worker',
      paneKey: 'tab_worker:eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
      processIncarnation: 'worker:1',
      worktreeId: 'repo::worker',
      effects: [],
      setupState: 'not_applicable',
      terminalOwnership: 'created'
    })

    expect(() => db.cancelTask(task.id, 'cancelled', { reason: 'must stop first' })).toThrowError(
      expect.objectContaining({
        code: 'task_not_startable',
        data: { taskId: task.id, dispatchId: started.dispatch.id }
      })
    )
    expect(db.getTask(task.id)).toMatchObject({ status: 'dispatched', terminal_reason: null })
    expect(db.getDispatchContextById(started.dispatch.id)?.status).toBe('pending')
  })

  it.each(['cancelled', 'superseded', 'completed', 'failed'] as const)(
    'is an idempotent no-op once the Task is already terminal (%s)',
    (priorStatus) => {
      const { db } = createDatabase()
      const task = db.createTask({ spec: 'already-terminal work' })
      if (priorStatus === 'cancelled' || priorStatus === 'superseded') {
        db.cancelTask(task.id, priorStatus, { reason: 'first call' })
      } else {
        db.updateTaskStatus(task.id, priorStatus, 'first call')
      }

      const result = db.cancelTask(task.id, 'cancelled', { reason: 'second call must not apply' })

      expect(result?.status).toBe(priorStatus)
      expect(result?.terminal_reason).not.toBe('second call must not apply')
    }
  )

  it('rolls back a supersede when Dispatch settlement fails', () => {
    const { db } = createDatabase()
    const task = db.createTask({ spec: 'atomic supersede' })
    const dispatch = db.createDispatchContext(task.id, 'term_worker')
    sqliteFor(db).exec(`
      CREATE TRIGGER reject_cancel_settlement
      BEFORE UPDATE OF status ON dispatch_contexts
      WHEN OLD.id = '${dispatch.id}'
      BEGIN
        SELECT RAISE(ABORT, 'forced dispatch settlement failure');
      END;
    `)

    expect(() => db.cancelTask(task.id, 'superseded', { reason: 'must roll back' })).toThrow(
      'forced dispatch settlement failure'
    )
    expect(db.getTask(task.id)).toMatchObject({
      status: 'dispatched',
      terminal_reason: null,
      completed_at: null
    })
    expect(db.getDispatchContextById(dispatch.id)).toMatchObject({
      status: 'dispatched',
      completed_at: null
    })
  })

  it('cascades cancellation to a pending dependent instead of leaving it stuck forever', () => {
    // Why: a pending dependent's readiness CASE requires the dependency to reach exactly
    // 'completed' — a cancelled dependency can never satisfy that, so leaving it 'pending' would
    // make evaluateDagConvergence count it as permanently "active" and the Run would never
    // converge or warn (#14548).
    const { db } = createDatabase()
    const dependency = db.createTask({ spec: 'cancelled dependency' })
    const dependent = db.createTask({ spec: 'waits on dependency', deps: [dependency.id] })
    expect(dependent.status).toBe('pending')

    db.cancelTask(dependency.id, 'cancelled', { reason: 'scope cut' })

    expect(db.getTask(dependency.id)?.status).toBe('cancelled')
    expect(db.getTask(dependent.id)).toMatchObject({
      status: 'cancelled',
      terminal_reason: expect.stringContaining(dependency.id)
    })
  })

  it('cascades supersede transitively through a multi-hop pending chain', () => {
    const { db } = createDatabase()
    const root = db.createTask({ spec: 'root work' })
    const middle = db.createTask({ spec: 'middle work', deps: [root.id] })
    const leaf = db.createTask({ spec: 'leaf work', deps: [middle.id] })

    db.cancelTask(root.id, 'superseded', { reason: 'replaced' })

    expect(db.getTask(middle.id)?.status).toBe('superseded')
    expect(db.getTask(leaf.id)?.status).toBe('superseded')
  })

  it('does not cascade past a dependent that already left pending', () => {
    const { db } = createDatabase()
    const dependencyA = db.createTask({ spec: 'dependency A' })
    const dependencyB = db.createTask({ spec: 'dependency B' })
    const dependent = db.createTask({
      spec: 'waits on both',
      deps: [dependencyA.id, dependencyB.id]
    })
    db.updateTaskStatus(dependencyB.id, 'failed', 'unrelated failure')
    db.updateTaskStatus(dependent.id, 'blocked', 'manual hold')

    db.cancelTask(dependencyA.id, 'cancelled', { reason: 'scope cut' })

    // Why: only 'pending' tasks are eligible — a task already off 'pending' for an unrelated
    // reason keeps whatever status put it there instead of being silently overwritten.
    expect(db.getTask(dependent.id)?.status).toBe('blocked')
  })
})

function createDatabase(path?: string): DatabaseHarness {
  const dir = path ? harnesses.find((harness) => harness.path === path)?.dir : undefined
  const ownedDir = dir ?? mkdtempSync(join(tmpdir(), 'orca-task-cancel-db-'))
  const dbPath = path ?? join(ownedDir, 'orchestration.db')
  const harness = { db: new OrchestrationDb(dbPath), dir: ownedDir, path: dbPath }
  harnesses.push(harness)
  return harness
}

function sqliteFor(db: OrchestrationDb): Database.Database {
  return (db as unknown as { db: Database.Database }).db
}
