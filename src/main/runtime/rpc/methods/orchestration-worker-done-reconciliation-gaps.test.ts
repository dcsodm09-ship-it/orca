import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RpcContext } from '../core'
import { createOrchestrationRpcHarness } from './orchestration-rpc-test-harness'
import type { OrchestrationDb } from '../../orchestration/db'
import type { OrcaRuntimeService } from '../../orca-runtime'

// #13364 / #7429: a worker_done that never reaches reconcileLifecycleMessage (capability
// rejection) or that omits taskId/dispatchId can leave a dispatch stuck instead of settling.
describe('orchestration.send worker_done reconciliation gaps', () => {
  const h = createOrchestrationRpcHarness()
  const { coordinatorPaneKey } = h
  let db: OrchestrationDb
  let runtime: OrcaRuntimeService
  let ctx: RpcContext
  let activeRunId: string | undefined

  function setup(): void {
    ;({ db, runtime, ctx, activeRunId } = h.setup())
  }

  afterEach(() => {
    h.cleanup()
  })

  async function call(name: string, params: Record<string, unknown>) {
    return h.call(name, params, ctx)
  }

  function readySupervisedDispatch(paneKey: string) {
    const task = db.createTask({ spec: 'capability work' })
    const started = db.createStartingWorkerDispatch({ taskId: task.id, startOptions: {} })
    db.prepareStartingWorkerAuthority({
      dispatchId: started.dispatch.id,
      handle: 'term_worker',
      paneKey,
      processIncarnation: 'runtime_test:term_worker:1',
      worktreeId: 'repo::worker',
      effects: [],
      setupState: 'not_applicable',
      terminalOwnership: 'created'
    })
    db.markWorkerDispatchReady(started.dispatch.id)
    return { task, dispatchId: started.dispatch.id }
  }

  it('marks a same-pane capability-rejected worker_done Dispatch stop_unknown instead of leaving it stuck (#13364)', async () => {
    setup()
    const { task, dispatchId } = readySupervisedDispatch('tab_worker:leaf_worker')
    vi.spyOn(runtime, 'getTerminalPaneKey').mockImplementation((handle) =>
      handle === 'term_worker' ? 'tab_worker:leaf_worker' : coordinatorPaneKey
    )
    const payload = JSON.stringify({ taskId: task.id, dispatchId, outcome: 'succeeded' })

    // ctx carries no orchestrationCapability, so the token check fails even though the pane matches.
    const rejected = (await call('orchestration.send', {
      from: 'term_worker',
      subject: 'Done',
      type: 'worker_done',
      payload
    })) as { lifecycle: { code: string } }

    expect(rejected.lifecycle.code).toBe('dispatch_capability_invalid')
    expect(db.getWorkerDispatch(dispatchId)?.state).toBe('stop_unknown')
    expect(db.getDispatchContextById(dispatchId)?.status).toBe('dispatched')
  })

  it('heals a stop_unknown worker row once a retried worker_done settles the task (#13364)', async () => {
    setup()
    const { task, dispatchId } = readySupervisedDispatch('tab_worker:leaf_worker')
    db.markWorkerReportRejectedUnknown(dispatchId, 'The Dispatch capability is missing.')
    expect(db.getWorkerDispatch(dispatchId)?.state).toBe('stop_unknown')

    // Mirrors what reconcileLifecycleMessage does once a retried worker_done clears
    // authority: the worker row must come back in sync with the task/dispatch it settled,
    // not stay stranded at stop_unknown after an accepted completion.
    expect(
      db.settleWorkerReport({
        taskId: task.id,
        dispatchId,
        outcome: 'succeeded',
        result: '{}'
      })
    ).toMatchObject({ action: 'settled', duplicate: false })

    expect(db.getWorkerDispatch(dispatchId)?.state).toBe('succeeded')
    expect(db.getTask(task.id)?.status).toBe('completed')
  })

  it('leaves worker state untouched for a capability-rejected worker_done from the wrong pane (#13364)', async () => {
    setup()
    const { task, dispatchId } = readySupervisedDispatch('tab_worker:leaf_worker')
    vi.spyOn(runtime, 'getTerminalPaneKey').mockImplementation((handle) =>
      handle === 'term_worker' ? 'tab_foreign:leaf_foreign' : coordinatorPaneKey
    )
    const payload = JSON.stringify({ taskId: task.id, dispatchId, outcome: 'succeeded' })

    const rejected = (await call('orchestration.send', {
      from: 'term_worker',
      subject: 'Done',
      type: 'worker_done',
      payload
    })) as { lifecycle: { code: string } }

    expect(rejected.lifecycle.code).toBe('dispatch_capability_invalid')
    expect(db.getWorkerDispatch(dispatchId)?.state).toBe('ready')
  })

  it("adopts the sender's sole active Dispatch when worker_done omits taskId/dispatchId, persisting it (#7429)", async () => {
    setup()
    const task = db.createTask({ spec: 'implicit dispatch work' })
    const dispatch = db.createDispatchContext(task.id, 'term_worker', 'tab_worker:leaf_worker')
    vi.spyOn(runtime, 'getTerminalPaneKey').mockImplementation((handle) =>
      handle === 'term_worker' ? 'tab_worker:leaf_worker' : coordinatorPaneKey
    )

    const result = (await call('orchestration.send', {
      from: 'term_worker',
      subject: 'Done',
      type: 'worker_done',
      payload: JSON.stringify({ outcome: 'succeeded' })
    })) as { lifecycle: { action: string }; message: { id: string } }

    expect(result.lifecycle).toMatchObject({ action: 'completed' })
    expect(db.getTask(task.id)?.status).toBe('completed')
    // Why: the adopted ids must be written back to the row, not just used in-memory for this
    // call — a later replay (e.g. orchestration.check re-reading it) re-parses payload from
    // scratch with no fallback resolution of its own.
    const persisted = db.getMessageById(result.message.id)
    expect(JSON.parse(persisted?.payload ?? '{}')).toMatchObject({
      taskId: task.id,
      dispatchId: dispatch.id,
      outcome: 'succeeded'
    })
  })

  it('rejects missing_task_id when the sender has no active Dispatch to fall back to (#7429)', async () => {
    setup()
    const result = (await call('orchestration.send', {
      from: 'term_worker',
      to: `run:${activeRunId}`,
      subject: 'Done',
      type: 'worker_done',
      payload: JSON.stringify({ outcome: 'succeeded' })
    })) as { lifecycle: { action: string; code: string } }

    expect(result.lifecycle).toMatchObject({ action: 'rejected', code: 'missing_task_id' })
  })
})
