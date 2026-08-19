/** Picking worker terminals, sending a task's dispatch preamble, and warning about hung dispatches. */
import type { OrchestrationDb } from './db'
import type { DispatchContextRow, TaskRow } from './types'
import { buildDispatchPreamble } from './preamble'
import type { CoordinatorRuntime, WorktreeDrift } from './coordinator-runtime-contract'
import {
  DISPATCH_STALE_THRESHOLD,
  parseAllowStaleBaseFromSpec
} from './coordinator-stale-base-flag'

export type TaskDispatchResult = 'dispatched' | 'stale-base-refused'

// Why: 10 min = documented heartbeat cadence (5 min) × 2, so one missed heartbeat is the earliest a dispatch can look stale.
const HUNG_THRESHOLD_MS = 10 * 60 * 1000

// Why: unique per call so nested/concurrent escalations (unlikely but not impossible on a single
// synchronous db handle) never collide on the same SAVEPOINT name.
let escalateStaleDispatchSavepointCounter = 0

// Why (#14829): mirrors orca-runtime.ts's failActiveDispatchOnExit — omitting dispatchId keeps
// applyEscalationToDispatch's payload check from ever failing the dispatch off staleness alone;
// this only makes the fact visible to handleEscalation and the LLM's check --wait mailbox.
// Why (#14829/#10673): resolve `to` and insert *before* stamping — stamping first left a dispatch
// with no resolvable target (the common case for the legacy Run) permanently unescalated, since
// nothing ever un-stamps it short of a heartbeat.
// Why: the insert+stamp pair is wrapped in one SAVEPOINT (mirrors insertMessages' own pattern) so
// a failure between them can't leave a delivered escalation un-stamped (duplicate next sweep) or
// a stamp with no delivered message (silently dropped forever, the original #14829/#10673 bug).
function escalateStaleDispatch(
  db: OrchestrationDb,
  ctx: DispatchContextRow,
  minutes: number
): void {
  const run = db.getRun(ctx.run_id)
  const modernRun = run && run.legacy !== 1 ? run : undefined
  const legacyRun = modernRun ? undefined : db.getActiveCoordinatorRun()
  const to = modernRun ? `run:${modernRun.id}` : legacyRun?.coordinator_handle
  if (!to) {
    return
  }
  const savepoint = `escalate_stale_dispatch_${escalateStaleDispatchSavepointCounter++}`
  db.db.exec(`SAVEPOINT ${savepoint}`)
  try {
    db.insertMessage({
      from: ctx.assignee_handle ?? `dispatch:${ctx.id}`,
      to,
      ...(modernRun ? { runId: modernRun.id } : {}),
      subject: `Dispatch has not reported in ~${minutes} min (no worker_done or heartbeat)`,
      type: 'escalation',
      priority: 'high',
      payload: JSON.stringify({ taskId: ctx.task_id, handle: ctx.assignee_handle })
    })
    db.markDispatchStaleEscalated(ctx.id, new Date().toISOString())
    db.db.exec(`RELEASE ${savepoint}`)
  } catch (error) {
    db.db.exec(`ROLLBACK TO ${savepoint}`)
    db.db.exec(`RELEASE ${savepoint}`)
    throw error
  }
}

// Why: warn only, never auto-fail — a false positive (slow but correct worker) costs more than a false negative (hung worker holding a slot); see R6 of DESIGN_DOC_PREAMBLE_FIX.md.
// Why (#10673): scopeRunIds is undefined for the legacy Coordinator loop (which owns the whole DB)
// and a caller-bound Set for the orchestration.check sweep, so a check call can't escalate dispatches
// on Runs it has nothing to do with.
export function warnStaleDispatches(
  db: OrchestrationDb,
  onLog: (msg: string) => void,
  scopeRunIds?: ReadonlySet<string>
): void {
  const thresholdIso = new Date(Date.now() - HUNG_THRESHOLD_MS).toISOString()
  const stale = db.getStaleDispatches(thresholdIso)
  const minutes = Math.round(HUNG_THRESHOLD_MS / 60000)
  for (const ctx of stale) {
    if (scopeRunIds && !scopeRunIds.has(ctx.run_id)) {
      continue
    }
    onLog(
      `Warning: worker ${ctx.assignee_handle ?? '<unknown>'} on task ${ctx.task_id} has not sent a heartbeat in ~${minutes} min (dispatch ${ctx.id})`
    )
    // Why: fire once per stale spell — getStaleDispatches keeps returning this row every tick until it heals or settles.
    if (!ctx.stale_escalated_at) {
      escalateStaleDispatch(db, ctx, minutes)
    }
  }
}

export async function listAvailableWorkerTerminals(
  db: OrchestrationDb,
  runtime: CoordinatorRuntime,
  coordinatorHandle: string,
  worktree: string | undefined
): Promise<string[]> {
  try {
    const result = await runtime.listTerminals(worktree, undefined, {
      includeVisualLayouts: false
    })
    const dispatched = db.listTasks({ status: 'dispatched' })
    const busyHandles = new Set<string>()

    for (const task of dispatched) {
      const ctx = db.getDispatchContext(task.id)
      if (ctx?.assignee_handle) {
        busyHandles.add(ctx.assignee_handle)
      }
    }

    // Why: createDispatchContext's dispatch-lock guarantees correctness; this filter is only an optimization to skip busy/disconnected terminals.
    return result.terminals
      .filter(
        (t) =>
          t.handle !== coordinatorHandle && !busyHandles.has(t.handle) && t.connected && t.writable
      )
      .map((t) => t.handle)
  } catch {
    return []
  }
}

export async function dispatchTaskToWorker(params: {
  db: OrchestrationDb
  runtime: CoordinatorRuntime
  task: TaskRow
  targetHandle: string
  baseDrift: WorktreeDrift
  coordinatorHandle: string
  worktree: string | undefined
  onLog: (msg: string) => void
  // Why: the coordinator owns the failed-task list, so a circuit break is reported back instead of mutated here.
  onCircuitBroken: (taskId: string) => void
}): Promise<TaskDispatchResult> {
  const { db, runtime, task, targetHandle, baseDrift, onLog } = params
  // Why (§3.1): drift check runs before createDispatchContext so a refusal doesn't bump failure_count (carried forward as MAX in db.ts:301-306) and burn the circuit-breaker budget; the task stays `ready` and retries next tick.
  const { allowStale, strippedSpec } = parseAllowStaleBaseFromSpec(task.spec)

  if (!params.worktree) {
    // Why (§7.4): worktree is optional; with none we can't probe drift, so log that the guard is inert and proceed.
    onLog(`stale-base guard inert for ${task.id}: coordinator has no worktree selector`)
  } else if (baseDrift && baseDrift.behind > DISPATCH_STALE_THRESHOLD && !allowStale) {
    // Why (§3.1): silent-return, not failDispatch — failing a recoverable stale-base here would burn the circuit-breaker budget.
    onLog(
      `Skipping dispatch of ${task.id}: worktree is ${baseDrift.behind} commits ` +
        `behind ${baseDrift.base}. Pull/rebase the worktree, recreate it with ` +
        `--base-branch ${baseDrift.base}, or include 'allow-stale-base: true' ` +
        `in the task spec to override. Task remains in 'ready'; coordinator ` +
        `will retry on the next tick.`
    )
    return 'stale-base-refused'
  }

  const dispatchAuthority = runtime.getOrchestrationDispatchAuthority?.(targetHandle)
  const assigneePaneKey =
    dispatchAuthority?.paneKey ?? runtime.getTerminalPaneKey?.(targetHandle) ?? undefined
  const processIncarnation =
    dispatchAuthority?.paneKey && dispatchAuthority.processIncarnation
      ? dispatchAuthority.processIncarnation
      : undefined
  const dispatch = db.createDispatchContext(
    task.id,
    targetHandle,
    assigneePaneKey,
    dispatchAuthority?.launchTokenHash ?? undefined,
    processIncarnation
  )

  // Why: dispatched agents use orca-dev in dev mode to reach the dev runtime's socket, not production (Section 6.4).
  const preamble = buildDispatchPreamble({
    taskId: task.id,
    dispatchId: dispatch.id,
    // Why (§3.4): strippedSpec drops the allow-stale-base line so the worker doesn't read the infra flag as an instruction.
    taskSpec: strippedSpec,
    coordinatorHandle: params.coordinatorHandle,
    workerHandle: targetHandle,
    devMode: process.env.ORCA_USER_DATA_PATH?.includes('orca-dev'),
    ...(runtime.getTerminalOrchestrationCliCommand
      ? { cliCommand: runtime.getTerminalOrchestrationCliCommand(targetHandle) }
      : {}),
    // Why (§3.2): pass baseDrift unconditionally — the preamble builder itself gates the drift section on behind > 0.
    ...(baseDrift ? { baseDrift } : {})
  })

  // Why: surface a since-resolved decision gate's outcome to the worker via the preamble.
  const gates = db.listGates({ taskId: task.id, status: 'resolved' })
  let gateContext = ''
  if (gates.length > 0) {
    const latest = gates.at(-1)!
    gateContext = `\n\n--- DECISION GATE RESOLVED ---\nQuestion: ${latest.question}\nResolution: ${latest.resolution}\n---\n`
  }

  try {
    await runtime.sendTerminalAgentPrompt(targetHandle, preamble + gateContext)
  } catch (err) {
    const updated = db.failDispatch(dispatch.id, err instanceof Error ? err.message : String(err))
    if (updated?.status === 'circuit_broken') {
      params.onCircuitBroken(task.id)
    }
    throw err
  }

  onLog(`Dispatched task ${task.id} to ${targetHandle}`)
  return 'dispatched'
}
