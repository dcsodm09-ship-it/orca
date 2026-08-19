import { OrchestrationError } from '../../orchestration-error'
import type { TaskRow } from '../../types'
import { isTerminalTaskStatus } from '../../types'
import { settleActiveDispatchesForTask } from '../dispatch-context/dispatch-completion'
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
function cascadeCancelPendingDependents(
  db: OrchestrationDb,
  cancelledTaskId: string,
  status: 'cancelled' | 'superseded',
  reason: string
): void {
  const pending = db.db.prepare("SELECT * FROM tasks WHERE status = 'pending'").all() as TaskRow[]
  for (const task of pending) {
    const deps: string[] = JSON.parse(task.deps)
    if (!deps.includes(cancelledTaskId)) {
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

// Why: first-class terminal transition for deliberately stopped/replaced work (#14548) — atomically
// fences any active context-only Dispatch instead of asking callers to compose worker-stop + task-update.
// Idempotent: a Task already terminal (by cancel or otherwise) is left untouched, mirroring the
// updateTaskStatus guard that keeps a late actor from mutating a Task that has already settled (#11499).
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
           AND status NOT IN ('completed', 'failed', 'cancelled', 'superseded')
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
      if (task && isTerminalTaskStatus(task.status)) {
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
        : `Dependency ${id} was ${status}`
    )
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
