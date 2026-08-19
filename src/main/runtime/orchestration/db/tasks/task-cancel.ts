import { OrchestrationError } from '../../orchestration-error'
import type { TaskRow } from '../../types'
import { settleActiveDispatchesForTask } from '../dispatch-context/dispatch-completion'
import { walkReplacementPredecessorChain } from './task-store'
import type { OrchestrationDb } from '../orchestration-db'

const CANCEL_TASK_SAVEPOINT = 'cancel_task'

// Why: a 'pending' task's readiness CASE (createTask) and promoteReadyTasks both require every
// dependency to reach exactly 'completed' — a cancelled/superseded dependency can never satisfy
// that, so without this the dependent silently stays 'pending' forever, still counts as "active"
// in evaluateDagConvergence, and the Run never converges and never warns (#14548). Cascade the
// same terminal status forward instead, recursing so a multi-hop chain (C depends on B depends on
// A) is fully unstuck by cancelling one ancestor. Only 'pending' tasks are eligible: a task that
// already reached 'ready'/'dispatched' had its dependencies satisfied at the time, so cancelling
// one afterward does not retroactively invalidate it.
// Why (round 6): a superseded task WITH a replacement is different — the replacement continues
// the work under a new id, so a pending dependent must NOT be killed; it stays 'pending' and
// promoteReadyTasks/the readiness CASE (both extended below) follow the replacement_task_id chain
// to unstick it once the replacement actually completes. Only a bare cancellation, or a supersede
// with no replacement, means the work really isn't happening — those still cascade as before.
function cascadeCancelPendingDependents(
  db: OrchestrationDb,
  cancelledTaskId: string,
  status: 'cancelled' | 'superseded',
  reason: string,
  replacementTaskId?: string
): void {
  const pending = db.db.prepare("SELECT * FROM tasks WHERE status = 'pending'").all() as TaskRow[]
  for (const task of pending) {
    const deps: string[] = JSON.parse(task.deps)
    if (!deps.includes(cancelledTaskId)) {
      continue
    }
    if (status === 'superseded' && replacementTaskId) {
      continue
    }
    const result = db.db
      .prepare(
        `UPDATE tasks
         SET status = ?, terminal_reason = COALESCE(terminal_reason, ?),
             completed_at = COALESCE(completed_at, datetime('now'))
         WHERE id = ? AND status = 'pending'`
      )
      .run(status, reason, task.id)
    if (result.changes === 1) {
      cascadeCancelPendingDependents(db, task.id, status, reason)
    }
  }
}

// Why (#14548 round 7): a task that is itself serving as another task's `replacement_task_id`
// can settle two ways the one-hop replacement-chain logic elsewhere doesn't automatically react
// to on its own:
//  - it completes AFTER already being recorded as the replacement, but before anything re-checked
//    the original's dependents from THIS side of the link (e.g. it had already completed before
//    cancelTask ever recorded the link — promoteReadyTasks only fires from the completing side,
//    and only if the link existed yet when it ran).
//  - it settles WITHOUT completing (cancelled, or bare-superseded with no further replacement) —
//    the substitution didn't pan out, so the original's still-pending dependents must be
//    cascaded now, the same as if the original had been bare-cancelled with no replacement at
//    all. Deliberately excludes 'failed': task-status-transition.ts's own guard explicitly keeps
//    completed/failed→ready retry as a legitimate pattern (e.g. legacy A/B pinned-worker
//    reconciliation) - cascading an irreversible 'cancelled' onto the original's dependents in
//    reaction to a transient, retryable failure would out-live the retry (a later successful
//    retry can never revive an already-cancelled dependent), which a review round found and
//    reproduced as a real regression. A replacement stuck 'failed' with no retry ever coming
//    does leave the original's dependents 'pending' rather than cascading - a known, accepted,
//    safer-by-default trade-off (never wrongly destroy work over a maybe-temporary failure).
// Exported so both cancelTask (below) and updateTaskStatus (task-status-transition.ts) can call
// it after their own terminal transition commits.
// Why (round 8, fix for a real bug an independent review found and reproduced): a multi-hop
// chain (original -> replacement1 -> replacement2) calls this once for replacement1 too, when
// replacement1 itself gets superseded by replacement2 - at THAT point replacement1's own status
// is 'superseded' with a replacement_task_id set, which used to look identical to "this
// replacement failed" and wrongly cascaded 'cancelled' onto `original`'s dependents even when
// replacement2 (the chain's real end) had already completed successfully. A task that is itself
// superseded WITH a further replacement is not a known outcome yet — the chain continues, and
// whichever task eventually settles at the chain's TRUE end re-triggers this same function from
// there, which (via walkReplacementPredecessorChain) resolves every hop back to `original` at
// once. Deciding here, one hop at a time, is exactly what produced the bug.
export function reconcileReplacementOutcome(db: OrchestrationDb, settledTaskId: string): void {
  const settled = db.getTask(settledTaskId)
  if (!settled) {
    return
  }
  if (settled.status === 'superseded' && settled.replacement_task_id) {
    return
  }
  const chain = walkReplacementPredecessorChain(db, settledTaskId)
  chain.delete(settledTaskId)
  if (chain.size === 0) {
    return
  }
  if (settled.status === 'completed') {
    db.promoteReadyTasks(settledTaskId)
    return
  }
  // Why: only 'cancelled' and bare 'superseded' (no further replacement, already excluded above)
  // are genuinely final outcomes here - 'failed' is deliberately excluded (see the docstring
  // above); isTerminalTaskStatus alone would also match it.
  if (settled.status !== 'cancelled' && settled.status !== 'superseded') {
    return
  }
  for (const originalId of chain) {
    cascadeCancelPendingDependents(
      db,
      originalId,
      'cancelled',
      `Replacement chain for ${originalId} did not complete (ended at ${settledTaskId}: ${settled.status})`
    )
  }
}

// Why: first-class terminal transition for deliberately stopped/replaced work (#14548) — atomically
// fences any active context-only Dispatch instead of asking callers to compose worker-stop + task-update.
// Idempotent: a Task already terminal (by cancel or otherwise) is left untouched, mirroring the
// updateTaskStatus guard that keeps a late actor from mutating a Task that has already settled (#11499).
// Why 'failed' is NOT treated as terminal here (round 10, fix for a real bug 2 independent
// reviews confirmed): reconcileReplacementOutcome deliberately excludes 'failed' from its cascade
// trigger (see its own docstring) so a transiently-failed replacement's retry can still succeed -
// but without this, that trade-off had no working escape hatch. If a caller decides a 'failed'
// replacement really IS abandoned for good (no retry coming), calling cancelTask on it must
// actually finalize it and cascade, not silently no-op and report success-shaped output with the
// task still 'failed'. 'completed'/'cancelled'/'superseded' remain genuinely final here; only
// 'failed' is deliberately mutable through this specific function.
export function cancelTask(
  this: OrchestrationDb,
  id: string,
  status: 'cancelled' | 'superseded',
  options: { reason?: string; replacementTaskId?: string } = {}
): TaskRow | undefined {
  if (options.replacementTaskId) {
    const task = this.getTask(id)
    const replacement = this.getTask(options.replacementTaskId)
    if (task && (!replacement || replacement.run_id !== task.run_id)) {
      throw new Error(
        `Replacement task ${options.replacementTaskId} must belong to run ${task.run_id}`
      )
    }
  }
  this.db.exec(`SAVEPOINT ${CANCEL_TASK_SAVEPOINT}`)
  try {
    const update = this.db
      .prepare(
        `UPDATE tasks
         SET status = ?, terminal_reason = COALESCE(?, terminal_reason),
             replacement_task_id = COALESCE(?, replacement_task_id),
             completed_at = COALESCE(completed_at, datetime('now'))
         WHERE id = ?
           AND status NOT IN ('completed', 'cancelled', 'superseded')
           AND NOT EXISTS (
             SELECT 1
             FROM dispatch_contexts active
             JOIN worker_dispatches worker ON worker.dispatch_id = active.id
             WHERE active.task_id = tasks.id
               AND active.status IN ('pending', 'dispatched')
               AND worker.state NOT IN ('failed', 'succeeded', 'stopped', 'abandoned')
           )`
      )
      .run(status, options.reason ?? null, options.replacementTaskId ?? null, id)
    if (update.changes !== 1) {
      const task = this.getTask(id)
      // Why not isTerminalTaskStatus: 'failed' is deliberately excluded here too (see the
      // docstring above) - a 0-row result while the task is 'failed' means something else
      // blocked the write (e.g. an active worker), not that there's nothing left to do.
      if (
        task &&
        (task.status === 'completed' || task.status === 'cancelled' || task.status === 'superseded')
      ) {
        this.db.exec(`RELEASE ${CANCEL_TASK_SAVEPOINT}`)
        return task
      }
      const activeWorker = this.db
        .prepare(
          `SELECT active.id
           FROM dispatch_contexts active
           JOIN worker_dispatches worker ON worker.dispatch_id = active.id
           WHERE active.task_id = ? AND active.status IN ('pending', 'dispatched')
             AND worker.state NOT IN ('failed', 'succeeded', 'stopped', 'abandoned')
           ORDER BY active.rowid DESC LIMIT 1`
        )
        .get(id) as { id: string } | undefined
      if (task && activeWorker) {
        throw new OrchestrationError(
          'task_not_startable',
          `Task ${id} cannot move to ${status} while supervised Dispatch ${activeWorker.id} is active; stop or settle its worker first.`,
          { taskId: id, dispatchId: activeWorker.id }
        )
      }
      this.db.exec(`RELEASE ${CANCEL_TASK_SAVEPOINT}`)
      return task
    }
    // Why: a fenced dispatch is not "completed" — it never finished; mirror worker-dispatch-stop's
    // status='failed'/last_failure convention so callers (e.g. legacy retry dedup) see the truth (#14548).
    settleActiveDispatchesForTask(
      this,
      id,
      'failed',
      options.reason ? `Task ${status}: ${options.reason}` : `Task ${status}`
    )
    cascadeCancelPendingDependents(
      this,
      id,
      status,
      options.reason
        ? `Dependency ${id} was ${status}: ${options.reason}`
        : `Dependency ${id} was ${status}`,
      options.replacementTaskId
    )
    // Why (#14548 round 7, GAP 1): if the replacement just linked here already completed BEFORE
    // this call ever recorded the link (its own promoteReadyTasks ran with no link to find yet),
    // re-check now that the link exists.
    if (options.replacementTaskId) {
      reconcileReplacementOutcome(this, options.replacementTaskId)
    }
    // Why (#14548 round 7, GAP 2): `id` may itself be serving as some OTHER task's replacement -
    // if this cancel/supersede means `id` will never complete either, that original's still-
    // pending dependents need cascading now, same as if there had been no replacement at all.
    reconcileReplacementOutcome(this, id)
    const task = this.getTask(id)
    this.db.exec(`RELEASE ${CANCEL_TASK_SAVEPOINT}`)
    return task
  } catch (error) {
    this.db.exec(`ROLLBACK TO ${CANCEL_TASK_SAVEPOINT}`)
    this.db.exec(`RELEASE ${CANCEL_TASK_SAVEPOINT}`)
    throw error
  }
}

export type TaskCancelMethods = {
  cancelTask: typeof cancelTask
}

export function attachTaskCancel(ctor: { prototype: object }): void {
  Object.assign(ctor.prototype, { cancelTask })
}
