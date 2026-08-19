import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { OrchestrationDb } from './db'

// Why (#15048 follow-up): a worker orphaned in 'stopping' by a crash mid-orchestration.workerStop
// must self-heal on the next launch — otherwise it is permanently stuck, since beginWorkerStop
// refuses to re-enter from 'stopping' and failDispatch/failActiveDispatchOnExit now deliberately
// refuse to auto-fail a dispatch whose worker might still be mid-stop.
describe('worker-dispatch stop orphan reconciliation on startup', () => {
  let db: OrchestrationDb | undefined
  let tempDir: string | undefined

  afterEach(() => {
    db?.close()
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('downgrades a worker orphaned in stopping to stop_unknown on the next process launch', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-worker-stop-orphan-'))
    const dbPath = join(tempDir, 'orchestration.db')

    db = new OrchestrationDb(dbPath)
    const task = db.createTask({ spec: 'stop mid-flight' })
    const started = db.createStartingWorkerDispatch({ taskId: task.id, startOptions: {} })
    db.prepareStartingWorkerAuthority({
      dispatchId: started.dispatch.id,
      handle: 'term_worker',
      paneKey: 'tab_worker:leaf_worker',
      processIncarnation: 'runtime:pty:1',
      worktreeId: 'repo::worktree',
      setupState: 'not_applicable',
      effects: []
    })
    db.markWorkerDispatchReady(started.dispatch.id)
    expect(db.beginWorkerStop(started.dispatch.id).disposition).toBe('stopping')
    // Why: simulate the app quitting/crashing before settleWorkerStop ever runs.
    db.close()

    db = new OrchestrationDb(dbPath)
    const worker = db.getWorkerDispatch(started.dispatch.id)
    expect(worker?.state).toBe('stop_unknown')

    // Why: the exit-driven safety net (failDispatch/failActiveDispatchOnExit) is only
    // unblocked once the worker leaves 'stopping' — confirm it can now settle the Dispatch.
    const failed = db.failDispatch(started.dispatch.id, 'Agent exited with code 1', {
      workerProcessExited: true
    })
    expect(failed?.status).toBe('failed')
  })

  it('leaves a worker mid-stop alone across a plain reopen within the same lifetime intent', () => {
    tempDir = mkdtempSync(join(tmpdir(), 'orca-worker-stop-settled-'))
    const dbPath = join(tempDir, 'orchestration.db')

    db = new OrchestrationDb(dbPath)
    const task = db.createTask({ spec: 'clean stop' })
    const started = db.createStartingWorkerDispatch({ taskId: task.id, startOptions: {} })
    db.prepareStartingWorkerAuthority({
      dispatchId: started.dispatch.id,
      handle: 'term_worker',
      paneKey: 'tab_worker:leaf_worker',
      processIncarnation: 'runtime:pty:1',
      worktreeId: 'repo::worktree',
      setupState: 'not_applicable',
      effects: []
    })
    db.markWorkerDispatchReady(started.dispatch.id)
    db.beginWorkerStop(started.dispatch.id)
    db.settleWorkerStop(started.dispatch.id)
    db.close()

    db = new OrchestrationDb(dbPath)
    // Why: a cleanly settled 'stopped' worker must not be touched by the orphan sweep.
    expect(db.getWorkerDispatch(started.dispatch.id)?.state).toBe('stopped')
  })
})
