import type { DispatchContextRow, WorkerDispatchRow } from '../../types'
import { OrchestrationError } from '../../orchestration-error'
import {
  releaseContextOnlyDispatch,
  type ContextOnlyDispatchReleaseResult
} from '../../context-only-dispatch-release'
import { isEquivalentPaneKey } from '../pane-key-match'
import type { OrchestrationDb } from '../orchestration-db'
import { reconcileTaskAfterDispatchInterruption } from '../dispatch-context/task-dispatch-reconciliation'

export function isDispatchProcessCurrent(
  this: OrchestrationDb,
  params: {
    dispatchId: string
    paneKey: string | null
    processIncarnation: string | null
  }
): boolean {
  const dispatch = this.getDispatchContextById(params.dispatchId)
  return Boolean(
    dispatch?.assignee_pane_key &&
    params.paneKey &&
    isEquivalentPaneKey(dispatch.assignee_pane_key, params.paneKey) &&
    dispatch.process_incarnation &&
    params.processIncarnation === dispatch.process_incarnation
  )
}

export function beginWorkerStop(
  this: OrchestrationDb,
  dispatchId: string
):
  | { disposition: 'stopping'; worker: WorkerDispatchRow; dispatch: DispatchContextRow }
  | { disposition: 'already_settled'; worker: WorkerDispatchRow; dispatch: DispatchContextRow }
  | ({ disposition: 'context_only' } & ContextOnlyDispatchReleaseResult) {
  this.db.exec('BEGIN IMMEDIATE')
  try {
    const dispatch = this.getDispatchContextById(dispatchId)
    const worker = this.getWorkerDispatch(dispatchId)
    if (!dispatch) {
      throw new OrchestrationError('dispatch_not_found', `Dispatch ${dispatchId} was not found.`)
    }
    if (!worker) {
      const released = releaseContextOnlyDispatch(this.db, dispatch, 'stopped')
      if (!released.alreadySettled) {
        this.closeQuestionsForDispatch(dispatchId)
      }
      this.db.exec('COMMIT')
      return { disposition: 'context_only', ...released }
    }
    if (['succeeded', 'failed', 'stopped', 'abandoned'].includes(worker.state)) {
      this.db.exec('COMMIT')
      return { disposition: 'already_settled', worker, dispatch }
    }
    // Why: 'stop_unknown' means a prior stop/report outcome was never confirmed (lost
    // contact, an orphaned restart sweep, or #13364's capability-miss rejection) — the
    // documented recovery path ("worker-stop --dispatch <id> and inspect again") depends
    // on re-entering 'stopping' from here, the same as the pre-existing 'start_unknown' case.
    if (!['ready', 'start_unknown', 'stop_unknown'].includes(worker.state)) {
      throw new OrchestrationError(
        'dispatch_inactive',
        `Dispatch ${dispatchId} cannot stop from ${worker.state}.`
      )
    }
    this.db
      .prepare(
        `UPDATE worker_dispatches
         SET state = 'stopping', stage = 'stop_requested', updated_at = datetime('now')
         WHERE dispatch_id = ? AND state IN ('ready', 'start_unknown', 'stop_unknown')`
      )
      .run(dispatchId)
    this.db
      .prepare(
        `UPDATE dispatch_contexts
         SET capability_revoked_at = COALESCE(capability_revoked_at, datetime('now'))
         WHERE id = ?`
      )
      .run(dispatchId)
    reconcileTaskAfterDispatchInterruption(this, dispatch.task_id, dispatchId)
    this.closeQuestionsForDispatch(dispatchId)
    this.db.exec('COMMIT')
    return {
      disposition: 'stopping',
      worker: this.getWorkerDispatch(dispatchId) as WorkerDispatchRow,
      dispatch: this.getDispatchContextById(dispatchId) as DispatchContextRow
    }
  } catch (error) {
    this.db.exec('ROLLBACK')
    throw error
  }
}

export function settleWorkerStop(this: OrchestrationDb, dispatchId: string): WorkerDispatchRow {
  this.db.exec('BEGIN IMMEDIATE')
  try {
    const worker = this.getWorkerDispatch(dispatchId)
    const dispatch = this.getDispatchContextById(dispatchId)
    if (!worker || !dispatch || worker.state !== 'stopping') {
      throw new OrchestrationError('dispatch_inactive', `Dispatch ${dispatchId} is not stopping.`)
    }
    this.db
      .prepare(
        `UPDATE worker_dispatches
         SET state = 'stopped', stage = 'process_stopped', updated_at = datetime('now')
         WHERE dispatch_id = ? AND state = 'stopping'`
      )
      .run(dispatchId)
    this.db
      .prepare(
        `UPDATE dispatch_contexts
         SET status = 'failed', completed_at = datetime('now'), last_failure = 'stopped'
         WHERE id = ? AND status IN ('pending', 'dispatched')`
      )
      .run(dispatchId)
    reconcileTaskAfterDispatchInterruption(this, dispatch.task_id, dispatchId)
    this.db.exec('COMMIT')
    return this.getWorkerDispatch(dispatchId) as WorkerDispatchRow
  } catch (error) {
    this.db.exec('ROLLBACK')
    throw error
  }
}

export function reconcileFederatedWorkerStop(
  this: OrchestrationDb,
  dispatchId: string
): WorkerDispatchRow {
  this.db.exec('BEGIN IMMEDIATE')
  try {
    const worker = this.getWorkerDispatch(dispatchId)
    const dispatch = this.getDispatchContextById(dispatchId)
    if (!worker || !dispatch || !this.getFederatedDispatch(dispatchId)) {
      throw new OrchestrationError(
        'dispatch_not_found',
        `Federated Dispatch ${dispatchId} was not found.`
      )
    }
    if (worker.state === 'stopped') {
      this.db.exec('COMMIT')
      return worker
    }
    if (!['stopping', 'stop_unknown'].includes(worker.state)) {
      throw new OrchestrationError(
        'dispatch_inactive',
        `Federated Dispatch ${dispatchId} cannot reconcile stop from ${worker.state}.`
      )
    }
    this.db
      .prepare(
        `UPDATE worker_dispatches
         SET state = 'stopped', stage = 'process_stopped', last_error = NULL,
             updated_at = datetime('now')
         WHERE dispatch_id = ? AND state IN ('stopping', 'stop_unknown')`
      )
      .run(dispatchId)
    this.db
      .prepare(
        `UPDATE dispatch_contexts
         SET status = 'failed', completed_at = COALESCE(completed_at, datetime('now')),
             last_failure = 'stopped'
         WHERE id = ? AND status IN ('pending', 'dispatched')`
      )
      .run(dispatchId)
    reconcileTaskAfterDispatchInterruption(this, dispatch.task_id, dispatchId)
    this.db.exec('COMMIT')
    return this.getWorkerDispatch(dispatchId) as WorkerDispatchRow
  } catch (error) {
    this.db.exec('ROLLBACK')
    throw error
  }
}

export function resumeFederatedWorkerForTerminalRelay(
  this: OrchestrationDb,
  dispatchId: string
): WorkerDispatchRow {
  this.db.exec('BEGIN IMMEDIATE')
  try {
    const worker = this.getWorkerDispatch(dispatchId)
    const dispatch = this.getDispatchContextById(dispatchId)
    if (!worker || !dispatch || worker.state !== 'stopping') {
      throw new OrchestrationError('dispatch_inactive', `Dispatch ${dispatchId} is not stopping.`)
    }
    this.db
      .prepare(
        `UPDATE worker_dispatches
         SET state = 'ready', stage = 'remote_report_pending', updated_at = datetime('now')
         WHERE dispatch_id = ? AND state = 'stopping'`
      )
      .run(dispatchId)
    this.db
      .prepare("UPDATE tasks SET status = 'dispatched' WHERE id = ? AND status = 'blocked'")
      .run(dispatch.task_id)
    this.db.exec('COMMIT')
    return this.getWorkerDispatch(dispatchId) as WorkerDispatchRow
  } catch (error) {
    this.db.exec('ROLLBACK')
    throw error
  }
}

export function markWorkerStopUnknown(
  this: OrchestrationDb,
  dispatchId: string,
  reason: string
): WorkerDispatchRow {
  const worker = this.getWorkerDispatch(dispatchId)
  if (!worker || worker.state !== 'stopping') {
    throw new OrchestrationError('dispatch_inactive', `Dispatch ${dispatchId} is not stopping.`)
  }
  this.db
    .prepare(
      `UPDATE worker_dispatches
       SET state = 'stop_unknown', stage = 'stop_outcome_unknown', last_error = ?,
           updated_at = datetime('now')
       WHERE dispatch_id = ? AND state = 'stopping'`
    )
    .run(reason, dispatchId)
  return this.getWorkerDispatch(dispatchId) as WorkerDispatchRow
}

// #13364: a worker_done rejected as dispatch_capability_invalid never reaches
// reconcileLifecycleMessage's settlement, so 'ready'/'starting' would otherwise
// sit forever with no dead-worker signal. Reuses 'stop_unknown' (no schema
// change) so existing stop/recovery paths already treat it as needing review.
// Callers must have already confirmed sender identity via hasLifecycleAuthority
// — this is a best-effort cleanup, so it no-ops rather than throws on a state
// that already moved on (e.g. a concurrent settlement or manual stop).
export function markWorkerReportRejectedUnknown(
  this: OrchestrationDb,
  dispatchId: string,
  reason: string
): WorkerDispatchRow | undefined {
  const worker = this.getWorkerDispatch(dispatchId)
  if (!worker || !['ready', 'starting'].includes(worker.state)) {
    return worker
  }
  this.db
    .prepare(
      `UPDATE worker_dispatches
       SET state = 'stop_unknown', stage = 'worker_report_rejected', last_error = ?,
           updated_at = datetime('now')
       WHERE dispatch_id = ? AND state IN ('ready', 'starting')`
    )
    .run(reason, dispatchId)
  return this.getWorkerDispatch(dispatchId)
}

// Why (#15048 follow-up): OrchestrationDb is opened once per runtime lifetime, so a
// worker_dispatches row still 'stopping' when this process starts cannot belong to a stop
// in flight in this process — it can only be orphaned by a previous lifetime's
// orchestration.workerStop that never reached settleWorkerStop (app quit/crash mid-flight).
// Left as 'stopping' it can never resolve on its own: beginWorkerStop refuses to re-enter, and
// failDispatch/failActiveDispatchOnExit now correctly refuse to auto-fail a dispatch whose
// worker might still be mid-stop. Downgrading to 'stop_unknown' here is the same
// outcome-unconfirmed state markWorkerStopUnknown already uses for every other lost-contact
// case; it is safe exactly because no live process can still be racing this transition.
export function reconcileOrphanedStoppingWorkersOnStartup(this: OrchestrationDb): void {
  this.db
    .prepare(
      `UPDATE worker_dispatches
       SET state = 'stop_unknown', stage = 'stop_outcome_unknown',
           last_error = 'App restarted while a stop was in flight; outcome unknown',
           updated_at = datetime('now')
       WHERE state = 'stopping'`
    )
    .run()
}

export type WorkerDispatchStopMethods = {
  isDispatchProcessCurrent: typeof isDispatchProcessCurrent
  beginWorkerStop: typeof beginWorkerStop
  settleWorkerStop: typeof settleWorkerStop
  reconcileFederatedWorkerStop: typeof reconcileFederatedWorkerStop
  resumeFederatedWorkerForTerminalRelay: typeof resumeFederatedWorkerForTerminalRelay
  markWorkerStopUnknown: typeof markWorkerStopUnknown
  markWorkerReportRejectedUnknown: typeof markWorkerReportRejectedUnknown
  reconcileOrphanedStoppingWorkersOnStartup: typeof reconcileOrphanedStoppingWorkersOnStartup
}

export function attachWorkerDispatchStop(ctor: { prototype: object }): void {
  Object.assign(ctor.prototype, {
    isDispatchProcessCurrent,
    beginWorkerStop,
    settleWorkerStop,
    reconcileFederatedWorkerStop,
    resumeFederatedWorkerForTerminalRelay,
    markWorkerStopUnknown,
    markWorkerReportRejectedUnknown,
    reconcileOrphanedStoppingWorkersOnStartup
  })
}
