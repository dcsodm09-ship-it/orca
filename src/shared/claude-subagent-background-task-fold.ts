import { AGENT_STATUS_MAX_SUBAGENTS } from './agent-status-types'
import type { ClaudeBackgroundAgentTask } from './claude-background-task-inventory'
import {
  CLAUDE_WAITING_SUBAGENT_STALE_MS,
  isClaudeTeammateLifecycleId,
  upsertWorkingClaudeSubagent,
  type ClaudeSubagentRoster,
  type TrackedClaudeSubagent
} from './claude-subagent-roster'

/** Fold a lead Stop's `background_tasks` into the lifecycle-tracked roster.
 *
 *  The list is authoritative for subagent-typed entries only: a running
 *  one-shot/workflow lane is always listed under its lifecycle `agent_id`,
 *  foreground children cannot span a lead Stop, and finished tasks drop from
 *  the list. Teammate-typed entries prove nothing per-agent (unrelated ids,
 *  permanently "running") — but their PRESENCE proves the session has
 *  named-agent/teammate machinery, and their total absence from a complete
 *  inventory proves no teammate-shaped child can still be alive. So:
 *  - an empty list proves nothing is left alive → clear the roster;
 *  - an id-exact subagent-typed match that is running is trusted fully and
 *    tagged listedAsSubagentTask; one reported not running is removed;
 *  - an unmatched RUNNING subagent-typed entry is a one-shot this listener
 *    never saw start (Orca/relay restart mid-run) → recreate it;
 *  - an unlisted entry is finished or dead (its SubagentStop was lost) →
 *    remove it — UNLESS it is teammate-shaped, live-tracked, never
 *    subagent-listed, the list still shows teammate-typed tasks, AND it is
 *    working, waiting, or TeammateIdle-confirmed: that is a named teammate
 *    whose id simply never appears, and removing it would drop the pane's
 *    done-gate (working), its pending approval card (waiting), or its parked
 *    idle row (confirmed). */
export function foldClaudeBackgroundTasksIntoRoster(
  roster: ClaudeSubagentRoster,
  tasks: ClaudeBackgroundAgentTask[],
  now: number,
  options?: { inventoryComplete?: boolean }
): void {
  if (tasks.length === 0) {
    if (options?.inventoryComplete !== false) {
      roster.clear()
    }
    return
  }
  const listedIds = new Set<string>()
  const pendingRunningTasks = new Map<string, ClaudeBackgroundAgentTask>()
  const hasTeammateTypedTask = tasks.some((task) => task.teammate)
  for (const task of tasks) {
    if (task.teammate) {
      continue
    }
    listedIds.add(task.id)
    const existing = roster.get(task.id)
    if (existing) {
      if (!task.running) {
        roster.delete(task.id)
        pendingRunningTasks.delete(task.id)
        continue
      }
      // Why: a Stop can park the row before the lead inventory confirms the
      // same workflow lane is still running; the authoritative task wins.
      // But background_tasks carries no per-task wait signal, so it can never
      // itself confirm a pending wait either — a 'waiting' row must stay
      // 'waiting' here, or a still-pending approval card silently vanishes.
      if (existing.state !== 'waiting') {
        existing.state = 'working'
      }
      existing.agentType = task.agentType ?? existing.agentType
      existing.description = task.description ?? existing.description
      existing.listedAsSubagentTask = true
      // Why: a live inventory listed the id as running — the restored claim is
      // now confirmed by the current process, so liveness can't reap it.
      existing.restoredFromSnapshot = undefined
      continue
    }
    if (!task.running) {
      pendingRunningTasks.delete(task.id)
      continue
    }
    upsertWorkingClaudeSubagent(
      roster,
      task.id,
      { agentType: task.agentType, description: task.description },
      now
    )
    const created = roster.get(task.id)
    if (created) {
      created.backgroundTasksAuthoritative = true
      created.listedAsSubagentTask = true
    } else {
      // Why: a full roster may still contain stale entries that this same
      // inventory will reap. Retry after cleanup so a replacement stays live.
      pendingRunningTasks.set(task.id, task)
    }
  }
  if (options?.inventoryComplete !== false) {
    for (const [id, tracked] of roster) {
      if (listedIds.has(id)) {
        continue
      }
      if (
        hasTeammateTypedTask &&
        !tracked.backgroundTasksAuthoritative &&
        tracked.listedAsSubagentTask !== true &&
        isClaudeTeammateLifecycleId(id) &&
        claudeSubagentSurvivesUnlistedFold(tracked, now)
      ) {
        continue
      }
      roster.delete(id)
    }
  }
  for (const task of pendingRunningTasks.values()) {
    if (roster.size >= AGENT_STATUS_MAX_SUBAGENTS) {
      break
    }
    upsertWorkingClaudeSubagent(
      roster,
      task.id,
      { agentType: task.agentType, description: task.description },
      now
    )
    const created = roster.get(task.id)
    if (created) {
      created.backgroundTasksAuthoritative = true
      created.listedAsSubagentTask = true
    }
  }
}

/** Whether an unlisted teammate-shaped row survives this fold's reap pass (see the
 *  doc comment above). A 'waiting' row is checked FIRST and gets only BOUNDED
 *  protection, regardless of confirmedTeammate/backgroundTasksAuthoritative or any
 *  other identity flag: background_tasks carries no per-task wait signal, so it can
 *  never itself reconfirm a pending wait — a leaked one (its SubagentStop/TeammateIdle
 *  never arrived) is forgiven for up to CLAUDE_WAITING_SUBAGENT_STALE_MS since its
 *  last live confirmation, then reaped, so it can no longer pin the pane 'waiting'
 *  forever (see waitingConfirmedAt). `confirmedTeammate` is a permanent IDENTITY flag
 *  ("this id belongs to a known named teammate"), not a liveness signal — checking it
 *  before 'waiting' would let every teammate past its first turn bypass this bound
 *  entirely, which is the common case this bound exists for, not a rare one.
 *  A non-waiting row: an idle row that no TeammateIdle ever confirmed is a finished
 *  workflow lane wearing a teammate-shaped id, reaped here or the pre-#8825 idle pile
 *  rebuilds one lane per lead turn; a 'working' or TeammateIdle-confirmed row is
 *  preserved unconditionally. */
function claudeSubagentSurvivesUnlistedFold(tracked: TrackedClaudeSubagent, now: number): boolean {
  if (tracked.state === 'waiting') {
    const confirmedAt = tracked.waitingConfirmedAt ?? tracked.startedAt
    return now - confirmedAt <= CLAUDE_WAITING_SUBAGENT_STALE_MS
  }
  return tracked.state === 'working' || tracked.confirmedTeammate === true
}
