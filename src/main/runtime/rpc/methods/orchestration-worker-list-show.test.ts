import { afterEach, describe, expect, it, vi } from 'vitest'
import { ORCHESTRATION_METHODS } from './orchestration'
import type { RpcContext } from '../core'
import { OrchestrationDb } from '../../orchestration/db'
import { OrcaRuntimeService } from '../../orca-runtime'

describe('orchestration worker-list / worker-show', () => {
  let db: OrchestrationDb
  let dbOpen = false
  let runtime: OrcaRuntimeService
  let ctx: RpcContext
  let activeRunId: string
  let inspectProcessLiveness: ReturnType<typeof vi.fn>

  const coordinatorPaneKey = 'tab_coord:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
  const workerPaneKey = 'tab_worker:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

  function setup(): void {
    db = new OrchestrationDb(':memory:')
    dbOpen = true
    runtime = new OrcaRuntimeService()
    runtime.setOrchestrationDb(db)
    inspectProcessLiveness = vi.fn().mockResolvedValue('live')
    ;(
      runtime as unknown as {
        inspectTerminalProcessIncarnationLiveness: typeof inspectProcessLiveness
      }
    ).inspectTerminalProcessIncarnationLiveness = inspectProcessLiveness
    vi.spyOn(runtime, 'getTerminalPaneKey').mockImplementation((handle) =>
      handle === 'term_coord'
        ? coordinatorPaneKey
        : handle === 'term_worker' || handle === 'term_reminted'
          ? workerPaneKey
          : null
    )
    vi.spyOn(runtime, 'getTerminalProcessIncarnation').mockImplementation((handle) =>
      handle === 'term_worker' || handle === 'term_reminted' ? 'runtime_test:term_worker:1' : null
    )
    vi.spyOn(runtime, 'getOrchestrationDispatchAuthority').mockImplementation((handle) =>
      handle === 'term_worker' || handle === 'term_reminted'
        ? ({
            terminalHandle: handle,
            paneKey: workerPaneKey,
            processIncarnation: 'runtime_test:term_worker:1',
            hostScope: { kind: 'local', hostId: 'local' }
          } as never)
        : null
    )
    vi.spyOn(runtime, 'validateOrchestrationAgentLauncher').mockImplementation(() => {})
    vi.spyOn(runtime, 'showTerminal').mockImplementation(
      async (handle) => ({ handle, worktreeId: 'repo::worktree', status: 'running' }) as never
    )
    vi.spyOn(runtime, 'showManagedTerminalWorkspace').mockResolvedValue({
      id: 'repo::worktree'
    } as never)
    vi.spyOn(runtime, 'createTerminal').mockResolvedValue({
      handle: 'term_worker',
      worktreeId: 'repo::worktree',
      title: 'worker'
    })
    vi.spyOn(runtime, 'waitForTerminal').mockResolvedValue({
      handle: 'term_worker',
      condition: 'tui-idle',
      satisfied: true,
      status: 'running',
      exitCode: null
    })
    vi.spyOn(runtime, 'getTerminalOrchestrationCliCommand').mockReturnValue('orca')
    vi.spyOn(runtime, 'sendTerminalAgentPrompt').mockResolvedValue({
      handle: 'term_worker',
      accepted: true,
      bytesWritten: 1
    })
    vi.spyOn(runtime, 'isTerminalRunningAgent').mockResolvedValue(true)
    vi.spyOn(runtime, 'getExactWorkerProviderSession').mockReturnValue(null)
    vi.spyOn(runtime, 'readTerminal').mockResolvedValue({
      handle: 'term_worker',
      status: 'running',
      tail: ['worker output line 1', 'worker output line 2'],
      truncated: false,
      nextCursor: '2'
    })
    vi.spyOn(runtime, 'closeTerminal').mockResolvedValue({
      handle: 'term_worker',
      tabId: 'tab-worker',
      ptyKilled: true
    } as never)
    vi.spyOn(runtime, 'notifyMessageArrived').mockImplementation(() => {})
    activeRunId = db.createRun({
      objective: 'Release test Run',
      coordinatorHandle: 'term_coord',
      coordinatorPaneKey
    }).id
    ctx = { runtime }
  }

  afterEach(() => {
    if (dbOpen) {
      dbOpen = false
      db.close()
    }
    vi.restoreAllMocks()
  })

  function findMethod(name: string) {
    const method = ORCHESTRATION_METHODS.find((m) => m.name === name)
    if (!method) {
      throw new Error(`Method not found: ${name}`)
    }
    return method
  }

  async function call(name: string, params: Record<string, unknown>) {
    const method = findMethod(name)
    const parsed = method.params ? method.params.parse(params) : undefined
    return method.handler(parsed, ctx)
  }

  async function startWorker(options: { terminal?: string } = {}): Promise<{
    taskId: string
    dispatchId: string
  }> {
    const task = db.createTask({ spec: 'release fixture task', runId: activeRunId })
    const result = (await call('orchestration.workerStart', {
      task: task.id,
      from: 'term_coord',
      ...(options.terminal ? { terminal: options.terminal } : { agent: 'codex' })
    })) as { dispatchId: string; state: string }
    expect(result.state).toBe('ready')
    return { taskId: task.id, dispatchId: result.dispatchId }
  }

  function settle(taskId: string, dispatchId: string, outcome: 'succeeded' | 'failed'): void {
    const settlement = db.settleWorkerReport({
      taskId,
      dispatchId,
      outcome,
      result: `worker ${outcome}`
    })
    expect(settlement.action).toBe('settled')
  }

  async function startSettledWorker(
    outcome: 'succeeded' | 'failed' = 'succeeded',
    options: { terminal?: string } = {}
  ): Promise<{ taskId: string; dispatchId: string }> {
    const worker = await startWorker(options)
    settle(worker.taskId, worker.dispatchId, outcome)
    return worker
  }

  it('worker-retain records a durable user exception that release can later replace', async () => {
    setup()
    const { dispatchId } = await startSettledWorker()
    const retained = (await call('orchestration.workerRetain', { dispatch: dispatchId })) as {
      state: string
      reason?: string
    }
    expect(retained).toMatchObject({ state: 'retained', reason: 'user_requested' })
    expect(db.getWorkerTerminalResourceByOwner(dispatchId)?.release_state).toBe('retained')

    const release = (await call('orchestration.workerRelease', { dispatch: dispatchId })) as {
      state: string
    }
    expect(release.state).toBe('released')
  })

  it('worker-list separates terminal accounting from Task outcome', async () => {
    setup()
    const active = await startWorker()
    const perWorkerLookup = vi.spyOn(db, 'getWorkerTerminalResourceByOwner')
    perWorkerLookup.mockClear()
    const result1 = (await call('orchestration.workerList', { run: activeRunId })) as {
      workers: { dispatchId: string; terminalState: string | null; workerState: string }[]
      counts: Record<string, number>
    }
    expect(result1.workers).toHaveLength(1)
    expect(result1.workers[0]).toMatchObject({
      dispatchId: active.dispatchId,
      terminalState: 'active',
      workerState: 'ready'
    })
    expect(perWorkerLookup).not.toHaveBeenCalled()

    settle(active.taskId, active.dispatchId, 'succeeded')
    const result2 = (await call('orchestration.workerList', {
      run: activeRunId,
      terminalState: 'reclaimable'
    })) as { workers: { dispatchId: string }[]; counts: Record<string, number> }
    expect(result2.workers.map((worker) => worker.dispatchId)).toEqual([active.dispatchId])
    expect(result2.counts).toMatchObject({ reclaimable: 1 })

    await call('orchestration.workerRelease', { dispatch: active.dispatchId })
    const result3 = (await call('orchestration.workerList', { run: activeRunId })) as {
      workers: { terminalState: string | null; workerState: string }[]
    }
    expect(result3.workers[0]).toMatchObject({
      terminalState: 'released',
      workerState: 'succeeded'
    })
  })

  it('worker-list surfaces agent/model and filters by --agent', async () => {
    setup()
    const claudeTask = db.createTask({ spec: 'launch a custom model', runId: activeRunId })
    const claudeStart = (await call('orchestration.workerStart', {
      task: claudeTask.id,
      from: 'term_coord',
      agent: 'claude',
      model: 'aws-bedrock-opus-5',
      effort: 'high'
    })) as { dispatchId: string; state: string }
    expect(claudeStart.state).toBe('ready')

    // Give the second worker its own terminal identity: the shared setup() mocks
    // always mint 'term_worker' with workerPaneKey, which the claude dispatch above
    // still actively owns (unsettled) — a second worker on that same identity would
    // hit the same conflict 'rejects exact reuse after release intent' guards against.
    const workerPaneKey2 = 'tab_worker2:dddddddd-dddd-4ddd-8ddd-dddddddddddd'
    vi.spyOn(runtime, 'getTerminalPaneKey').mockImplementation((handle) =>
      handle === 'term_coord'
        ? coordinatorPaneKey
        : handle === 'term_worker' || handle === 'term_reminted'
          ? workerPaneKey
          : handle === 'term_worker2'
            ? workerPaneKey2
            : null
    )
    vi.spyOn(runtime, 'getTerminalProcessIncarnation').mockImplementation((handle) =>
      handle === 'term_worker' || handle === 'term_reminted'
        ? 'runtime_test:term_worker:1'
        : handle === 'term_worker2'
          ? 'runtime_test:term_worker2:1'
          : null
    )
    vi.spyOn(runtime, 'getOrchestrationDispatchAuthority').mockImplementation((handle) =>
      handle === 'term_worker' || handle === 'term_reminted'
        ? ({
            terminalHandle: handle,
            paneKey: workerPaneKey,
            processIncarnation: 'runtime_test:term_worker:1',
            hostScope: { kind: 'local', hostId: 'local' }
          } as never)
        : handle === 'term_worker2'
          ? ({
              terminalHandle: handle,
              paneKey: workerPaneKey2,
              processIncarnation: 'runtime_test:term_worker2:1',
              hostScope: { kind: 'local', hostId: 'local' }
            } as never)
          : null
    )
    vi.spyOn(runtime, 'createTerminal').mockResolvedValueOnce({
      handle: 'term_worker2',
      worktreeId: 'repo::worktree',
      title: 'worker'
    })

    const codex = await startWorker()

    const all = (await call('orchestration.workerList', { run: activeRunId })) as {
      workers: { dispatchId: string; agent: string | null; model: string | null }[]
    }
    expect(all.workers).toContainEqual(
      expect.objectContaining({
        dispatchId: claudeStart.dispatchId,
        agent: 'claude',
        model: 'aws-bedrock-opus-5'
      })
    )
    expect(all.workers).toContainEqual(
      expect.objectContaining({
        dispatchId: codex.dispatchId,
        agent: 'codex',
        model: null
      })
    )

    const filtered = (await call('orchestration.workerList', {
      run: activeRunId,
      agent: 'claude'
    })) as { workers: { dispatchId: string }[]; counts: Record<string, number> }
    expect(filtered.workers.map((w) => w.dispatchId)).toEqual([claudeStart.dispatchId])
    // Counts stay unfiltered by agent, same terminal-state-only scope as the existing filter.
    expect(filtered.counts).toMatchObject({ active: 2 })

    const none = (await call('orchestration.workerList', {
      run: activeRunId,
      agent: 'nonexistent-agent'
    })) as { workers: unknown[] }
    expect(none.workers).toEqual([])
  })

  it('reports abandoned workers as retained instead of reclaimable', async () => {
    setup()
    const { dispatchId } = await startWorker()
    await call('orchestration.workerAbandon', { dispatch: dispatchId })

    const listed = (await call('orchestration.workerList', { run: activeRunId })) as {
      workers: { dispatchId: string; terminalState: string | null }[]
    }

    expect(listed.workers).toContainEqual(
      expect.objectContaining({ dispatchId, terminalState: 'retained' })
    )
    await expect(
      call('orchestration.workerRelease', { dispatch: dispatchId })
    ).resolves.toMatchObject({
      state: 'retained',
      reason: 'identity_unproven',
      processAction: 'none'
    })
    expect(runtime.closeTerminal).not.toHaveBeenCalled()
  })

  it('worker-show exposes the terminal resource', async () => {
    setup()
    const { dispatchId } = await startSettledWorker()
    const shown = (await call('orchestration.workerShow', { dispatch: dispatchId })) as {
      terminalResource: { ownershipState: string; releaseState: string } | null
    }
    expect(shown.terminalResource).toMatchObject({
      ownershipState: 'owned',
      releaseState: 'not_requested'
    })
  })
})
