import type Database from '../../../../sqlite/sync-database'
import type { TaskStatus, TaskRow } from '../../types'
import { buildOrchestrationTaskDisplayMetadata } from '../../../../../shared/orchestration-task-display'
import { LEGACY_RUN_ID } from '../contract-constants'
import { generateId } from '../generated-id'
import type { TaskRuntimeLineageRow } from '../run-list-page'
import type { OrchestrationDb } from '../orchestration-db'

// ── Tasks ──

export function createTask(
  this: OrchestrationDb,
  task: {
    spec: string
    taskTitle?: string
    displayName?: string
    deps?: string[]
    parentId?: string
    createdByTerminalHandle?: string
    createdByPaneKey?: string
    createdByProcessIncarnation?: string
    createdByRunGeneration?: number
    runId?: string
  }
): TaskRow {
  const runId = task.runId ?? LEGACY_RUN_ID
  this.requireRun(runId)
  if (task.parentId) {
    const parent = this.getTask(task.parentId)
    if (!parent || parent.run_id !== runId) {
      throw new Error(`Parent task ${task.parentId} must belong to run ${runId}`)
    }
  }
  for (const depId of task.deps ?? []) {
    const dependency = this.getTask(depId)
    if (!dependency || dependency.run_id !== runId) {
      throw new Error(`Dependency task ${depId} must belong to run ${runId}`)
    }
  }
  const id = generateId('task')
  const depsJson = JSON.stringify(task.deps ?? [])
  const display = buildOrchestrationTaskDisplayMetadata({
    spec: task.spec,
    taskTitle: task.taskTitle,
    displayName: task.displayName
  })
  this.db
    .prepare(
      `INSERT INTO tasks (
         id, run_id, parent_id, created_by_terminal_handle, created_by_pane_key,
         created_by_process_incarnation, created_by_run_generation,
         task_title, display_name, spec, status, deps
       ) VALUES (
         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
         CASE WHEN EXISTS (
           SELECT 1
           FROM json_each(?) requested
           LEFT JOIN tasks dependency ON dependency.id = requested.value
           WHERE dependency.id IS NULL
              OR dependency.run_id <> ?
              -- Why (#14548 round 6): a dependency that was superseded, with its replacement
              -- already completed, represents finished work under a different task id — treat
              -- it the same as 'completed' rather than leaving every future dependent stuck.
              OR (
                dependency.status <> 'completed'
                AND NOT (
                  dependency.status = 'superseded'
                  AND dependency.replacement_task_id IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM tasks replacement
                    WHERE replacement.id = dependency.replacement_task_id
                      AND replacement.status = 'completed'
                  )
                )
              )
         ) THEN 'pending' ELSE 'ready' END,
         ?
       )`
    )
    .run(
      id,
      runId,
      task.parentId ?? null,
      task.createdByTerminalHandle ?? null,
      task.createdByPaneKey ?? null,
      task.createdByProcessIncarnation ?? null,
      task.createdByRunGeneration ?? null,
      display.taskTitle || null,
      display.displayName || null,
      task.spec,
      depsJson,
      runId,
      depsJson
    )
  return this.db.prepare('SELECT * FROM tasks WHERE id = ?').get(id) as TaskRow
}

// Why: return the active creator Dispatch proof with the Task read; runtime still owns pane/process currency.
export function getTask(this: OrchestrationDb, id: string): TaskRow | undefined
export function getTask(
  this: OrchestrationDb,
  id: string,
  dispatchRunId: string
): TaskRuntimeLineageRow | undefined
export function getTask(
  this: OrchestrationDb,
  id: string,
  dispatchRunId?: string
): TaskRow | TaskRuntimeLineageRow | undefined {
  if (dispatchRunId === undefined) {
    return this.db.prepare('SELECT * FROM tasks WHERE id = ?').get(id) as TaskRow | undefined
  }
  return this.db
    .prepare(
      `SELECT t.*,
         creator.id AS creator_dispatch_id,
         creator.run_id AS creator_dispatch_run_id,
         creator.assignee_pane_key AS creator_dispatch_pane_key,
         creator.process_incarnation AS creator_dispatch_process_incarnation
       FROM tasks t
       LEFT JOIN dispatch_contexts creator ON creator.rowid = (
         SELECT candidate.rowid
         FROM dispatch_contexts candidate
         WHERE candidate.assignee_handle = t.created_by_terminal_handle
           AND candidate.run_id = ?
           AND candidate.status IN ('pending', 'dispatched')
         ORDER BY candidate.rowid DESC
         LIMIT 1
       )
       WHERE t.id = ?`
    )
    .get(dispatchRunId, id) as TaskRuntimeLineageRow | undefined
}

export function listTasks(
  this: OrchestrationDb,
  filter?: { status?: TaskStatus; ready?: boolean; runId?: string }
): TaskRow[] {
  const runWhere = filter?.runId ? 'run_id = ? AND ' : ''
  const runParams: Database.BindValue[] = filter?.runId ? [filter.runId] : []
  if (filter?.ready) {
    return this.db
      .prepare(`SELECT * FROM tasks WHERE ${runWhere}status = 'ready' ORDER BY created_at`)
      .all(...runParams) as TaskRow[]
  }
  if (filter?.status) {
    return this.db
      .prepare(`SELECT * FROM tasks WHERE ${runWhere}status = ? ORDER BY created_at`)
      .all(...runParams, filter.status) as TaskRow[]
  }
  if (filter?.runId) {
    return this.db
      .prepare('SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at')
      .all(filter.runId) as TaskRow[]
  }
  return this.db.prepare('SELECT * FROM tasks ORDER BY created_at').all() as TaskRow[]
}

// Why: the correlated indexed lookup avoids materializing every retained Dispatch before filtering Tasks.
export function listTasksWithDispatch(
  this: OrchestrationDb,
  filter?: {
    status?: TaskStatus
    ready?: boolean
    runId?: string
  }
): (TaskRow & {
  assignee_handle: string | null
  dispatch_id: string | null
  last_dispatch_status: string | null
  last_dispatch_failure: string | null
})[] {
  const whereClauses: string[] = []
  const params: Database.BindValue[] = []
  if (filter?.runId) {
    whereClauses.push('t.run_id = ?')
    params.push(filter.runId)
  }
  if (filter?.ready) {
    whereClauses.push("t.status = 'ready'")
  } else if (filter?.status) {
    whereClauses.push('t.status = ?')
    params.push(filter.status)
  }
  const where = whereClauses.length > 0 ? `WHERE ${whereClauses.join(' AND ')}` : ''
  // Why (#8984): a Dispatch that failed on exit reverts its Task to 'ready' with
  // no active Dispatch row, so the active-only join above goes blank — surface
  // the most recent Dispatch's outcome too, so a dead worker stays visible.
  const sql = `
    SELECT
      t.*,
      d.assignee_handle AS assignee_handle,
      d.id              AS dispatch_id,
      last_d.status       AS last_dispatch_status,
      last_d.last_failure AS last_dispatch_failure
    FROM tasks t
    LEFT JOIN dispatch_contexts d ON d.rowid = (
      SELECT candidate.rowid
      FROM dispatch_contexts candidate
      WHERE candidate.task_id = t.id
        AND candidate.status IN ('pending', 'dispatched')
      ORDER BY candidate.rowid DESC
      LIMIT 1
    )
    LEFT JOIN dispatch_contexts last_d ON last_d.rowid = (
      SELECT candidate.rowid
      FROM dispatch_contexts candidate
      WHERE candidate.task_id = t.id
      ORDER BY candidate.rowid DESC
      LIMIT 1
    )
    ${where}
    ORDER BY t.created_at
  `
  return this.db.prepare(sql).all(...params) as (TaskRow & {
    assignee_handle: string | null
    dispatch_id: string | null
    last_dispatch_status: string | null
    last_dispatch_failure: string | null
  })[]
}

// Why: runs in the status-update transaction, so a completed task never leaves its ready children unpromoted.
// Why (#14548 round 6): a pending task can depend on a task id that was superseded before it ever
// completed - its replacement completing is what actually unblocks the dependent, but the
// dependent's `deps` array still names the ORIGINAL (superseded) id, not the replacement. Without
// resolving that link, a completing replacement would never re-check dependents of the task it
// replaced.
// Why (round 8, fix for a real bug an independent review found): the whole predecessor chain is
// walked, not just one hop - a multi-hop supersession (original -> replacement1 -> replacement2)
// needs `original`'s dependents to promote too once replacement2 (the chain's true end) completes,
// not just replacement1's direct dependents. A one-hop-only version left a completed chain's
// earlier-hop dependents stuck 'pending' forever, AND (worse) reconcileReplacementOutcome's own
// cascade-on-failure path could wrongly cancel them before this ever got a chance to run, because
// it couldn't tell "chain still resolving" apart from "chain failed" without this same walk.
// Why exported: task-cancel.ts's reconcileReplacementOutcome needs the identical walk to find
// every original transitively linked through a multi-hop chain, not just the immediate one.
export function walkReplacementPredecessorChain(db: OrchestrationDb, taskId: string): Set<string> {
  const chain = new Set<string>([taskId])
  let frontier = [taskId]
  while (frontier.length > 0) {
    const placeholders = frontier.map(() => '?').join(',')
    const predecessors = db.db
      .prepare(
        `SELECT id FROM tasks WHERE status = 'superseded' AND replacement_task_id IN (${placeholders})`
      )
      .all(...frontier) as { id: string }[]
    const fresh = predecessors.map((p) => p.id).filter((id) => !chain.has(id))
    fresh.forEach((id) => chain.add(id))
    frontier = fresh
  }
  return chain
}

export function promoteReadyTasks(this: OrchestrationDb, completedTaskId: string): void {
  const triggerIds = walkReplacementPredecessorChain(this, completedTaskId)

  const candidates = this.db
    .prepare("SELECT * FROM tasks WHERE status = 'pending'")
    .all() as TaskRow[]

  for (const task of candidates) {
    const deps: string[] = JSON.parse(task.deps)
    if (!deps.some((depId) => triggerIds.has(depId))) {
      continue
    }

    const allDepsCompleted = deps.every((depId) => {
      let dep = this.getTask(depId)
      const seen = new Set<string>()
      while (dep?.status === 'superseded' && dep.replacement_task_id && !seen.has(dep.id)) {
        seen.add(dep.id)
        dep = this.getTask(dep.replacement_task_id)
      }
      return dep?.status === 'completed'
    })
    if (allDepsCompleted) {
      this.db.prepare("UPDATE tasks SET status = 'ready' WHERE id = ?").run(task.id)
    }
  }
}

export type TaskStoreMethods = {
  createTask: typeof createTask
  getTask: typeof getTask
  listTasks: typeof listTasks
  listTasksWithDispatch: typeof listTasksWithDispatch
  promoteReadyTasks: typeof promoteReadyTasks
}

export function attachTaskStore(ctor: { prototype: object }): void {
  Object.assign(ctor.prototype, {
    createTask,
    getTask,
    listTasks,
    listTasksWithDispatch,
    promoteReadyTasks
  })
}
