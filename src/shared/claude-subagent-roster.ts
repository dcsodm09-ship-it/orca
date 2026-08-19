import {
  AGENT_STATUS_MAX_SUBAGENTS,
  AGENT_STATUS_STALE_AFTER_MS,
  type AgentSubagentSnapshot
} from './agent-status-types'

/** Mirrors the wire-normalization id cap in agent-status-types. Enforced at
 *  upsert so an over-long id can't gate the pane 'working' while being
 *  invisible in the emitted snapshots (which drop such ids). */
const CLAUDE_SUBAGENT_ID_MAX_LENGTH = 64

/** Live subagents/teammates tracked for one Claude pane, keyed by the
 *  provider-assigned `agent_id` from SubagentStart/SubagentStop payloads.
 *  One-shot children (hyphen-free ids) are tracked only while working — their
 *  SubagentStop means finished and removes the row. Teammate-shaped ids are
 *  turn-based on claude 2.1.21x (`in_process_teammate`): SubagentStop /
 *  TeammateIdle fire at every TURN end while the teammate stays alive and
 *  resumable, so those rows flip to 'idle' instead of leaving; a later
 *  SubagentStart flips them back to working. Idle rows never gate the pane
 *  'working' (the #8825 idle-squat rule), and only TeammateIdle-confirmed
 *  ones survive a lead-Stop fold — see foldClaudeBackgroundTasksIntoRoster. */
export type ClaudeSubagentRoster = Map<string, TrackedClaudeSubagent>

export type TrackedClaudeSubagent = {
  agentType?: string
  description?: string
  startedAt: number
  /** 'idle' = teammate between mailbox turns: alive/resumable, row stays
   *  visible but must not gate the pane 'working'. 'waiting' = this child's own
   *  PermissionRequest/AskUserQuestion is unresolved — distinct from the pane-level
   *  single-slot wait pointer so a second sibling's wait can never silently erase
   *  a first sibling's still-pending one (#P1). */
  state: 'working' | 'idle' | 'waiting'
  /** Set only while state==='waiting': the blocked tool call captured once at
   *  wait-start. Lets a sibling's later resolution repoint the pane's single-slot
   *  wait pointer and tool-snapshot cache at this row without losing which tool
   *  it is actually blocked on. Cleared whenever the row leaves 'waiting'. */
  waitingToolName?: string
  waitingToolInput?: string
  waitingToolUseId?: string
  /** A TeammateIdle matched this id by name — proof it is a persistent
   *  in-process teammate, not a workflow lane that merely reuses the
   *  `a<name>-<hex>` id shape. Never cleared: identity can't change mid-life.
   *  Unconfirmed idle rows are reaped at the next complete lead-Stop fold so
   *  finished lanes can't rebuild the pre-#8825 idle pile. */
  confirmedTeammate?: true
  /** The id came from a persisted snapshot or background_tasks, not live
   *  lifecycle events, so it may be a phantom whose SubagentStop was never
   *  observed (Orca restart). A present complete task list omitting it
   *  removes it even when teammate-shaped, so it can't gate the pane
   *  'working' forever. Cleared once live activity re-tracks the id. */
  backgroundTasksAuthoritative?: boolean
  /** The row was rebuilt from a persisted snapshot at restore and no live event
   *  has confirmed it since, so the only thing backing it is a claim written by
   *  an agent process that may no longer exist. Cleared by any live activity on
   *  the id. Lets a liveness check reap it when that process is gone — the
   *  inventory reap alone needs the parent to speak, and an idle parent never
   *  does. */
  restoredFromSnapshot?: boolean
  /** A subagent-typed background task listed this lifecycle id id-exact
   *  (workflow/named lanes) — proof the task list tracks this id, so a later
   *  complete list omitting it means finished/killed even though the id is
   *  teammate-shaped. Never cleared: the listing mode of an id can't change
   *  mid-life. */
  listedAsSubagentTask?: true
  /** Wall-clock time this row was last confirmed 'waiting' by a live hook event
   *  (upsertWaitingClaudeSubagent). Set only while state==='waiting'; bounds how
   *  long foldClaudeBackgroundTasksIntoRoster's teammate-shaped-id protection may
   *  keep a 'waiting' row alive on its own (background_tasks carries no per-task
   *  wait signal, so it can never itself reconfirm a pending wait) — without this,
   *  a leaked wait (its SubagentStop/TeammateIdle never arrived) could pin the
   *  pane 'waiting' forever, now that any 'waiting' row unconditionally forces
   *  the pane state. Deliberately wall-clock, not a per-fold counter: folds can
   *  run far more often than any real chance for this specific child to resolve
   *  (e.g. many unrelated lead Stops in quick succession), so counting folds
   *  risks evicting a slow-but-genuinely-alive wait — elapsed real time doesn't.
   *  Cleared to undefined by any live touch that ends the wait (upsertWorkingClaudeSubagent,
   *  stopClaudeSubagent, idleClaudeTeammateByName). */
  waitingConfirmedAt?: number
}

/** How long a 'waiting' row may survive on foldClaudeBackgroundTasksIntoRoster's
 *  teammate-shaped-id protection alone, with no live reconfirmation, before it's
 *  treated as leaked and reaped — see TrackedClaudeSubagent.waitingConfirmedAt.
 *  Reuses the same established convention as AGENT_STATUS_STALE_AFTER_MS (the
 *  wire/mobile status projection's own "give up on a stale wait" bound) rather
 *  than a fresh magic number. */
export const CLAUDE_WAITING_SUBAGENT_STALE_MS = AGENT_STATUS_STALE_AFTER_MS

/** Agent-team/named-agent lifecycle ids are `a<name>-<hex>` while one-shot
 *  ids are hyphen-free (`a<hex>`). Such ids are never listed as task ids in
 *  `background_tasks`, so omission from the list proves nothing for them. */
export function isClaudeTeammateLifecycleId(id: string): boolean {
  const separator = id.lastIndexOf('-')
  return separator > 1 && id.startsWith('a') && /^[0-9a-f]+$/i.test(id.slice(separator + 1))
}

export function upsertWorkingClaudeSubagent(
  roster: ClaudeSubagentRoster,
  id: string,
  fields: { agentType?: string; description?: string },
  now: number
): void {
  if (id.length === 0 || id.length > CLAUDE_SUBAGENT_ID_MAX_LENGTH) {
    return
  }
  const existing = roster.get(id)
  if (existing) {
    existing.state = 'working'
    existing.agentType = fields.agentType ?? existing.agentType
    existing.description = fields.description ?? existing.description
    // Why: no longer waiting — drop the frozen blocked-tool snapshot so a stale
    // one can't be read back if this row is later re-marked waiting.
    existing.waitingToolName = undefined
    existing.waitingToolInput = undefined
    existing.waitingToolUseId = undefined
    // Why: live activity proves the lifecycle stream owns this id again;
    // background_tasks omission must stop reaping it (teammate-shaped ids
    // never appear there). The fold re-tags its own recreations after this.
    existing.backgroundTasksAuthoritative = undefined
    // Why: the live event proves the agent process behind the restored row is
    // still running it, so the liveness reap must stop treating it as a claim.
    existing.restoredFromSnapshot = undefined
    existing.waitingConfirmedAt = undefined
    return
  }
  // Why: beyond the wire cap extra rows would be invisible anyway; idle
  // teammates are the only safe eviction — never displace a working child.
  if (roster.size >= AGENT_STATUS_MAX_SUBAGENTS && !evictOldestIdleClaudeSubagent(roster)) {
    return
  }
  roster.set(id, {
    state: 'working',
    startedAt: now,
    agentType: fields.agentType,
    description: fields.description
  })
}

/** Like upsertWorkingClaudeSubagent, but for a child whose OWN PermissionRequest/
 *  AskUserQuestion event just fired: marks its row 'waiting' (not 'working') and
 *  freezes the blocked tool's identity so a later sibling wait can't erase it —
 *  see TrackedClaudeSubagent.state and claudeRosterFindWaitingSubagent. */
export function upsertWaitingClaudeSubagent(
  roster: ClaudeSubagentRoster,
  id: string,
  fields: {
    agentType?: string
    description?: string
    toolName?: string
    toolInput?: string
    toolUseId?: string
  },
  now: number
): void {
  if (id.length === 0 || id.length > CLAUDE_SUBAGENT_ID_MAX_LENGTH) {
    return
  }
  const existing = roster.get(id)
  if (existing) {
    existing.state = 'waiting'
    existing.agentType = fields.agentType ?? existing.agentType
    existing.description = fields.description ?? existing.description
    existing.waitingToolName = fields.toolName
    existing.waitingToolInput = fields.toolInput
    existing.waitingToolUseId = fields.toolUseId
    existing.backgroundTasksAuthoritative = undefined
    existing.restoredFromSnapshot = undefined
    // Why: a live PermissionRequest/AskUserQuestion is fresh proof this child is
    // still genuinely waiting — resets the bounded fold-leak fallback above.
    existing.waitingConfirmedAt = now
    return
  }
  if (roster.size >= AGENT_STATUS_MAX_SUBAGENTS && !evictOldestIdleClaudeSubagent(roster)) {
    return
  }
  roster.set(id, {
    state: 'waiting',
    startedAt: now,
    agentType: fields.agentType,
    description: fields.description,
    waitingToolName: fields.toolName,
    waitingToolInput: fields.toolInput,
    waitingToolUseId: fields.toolUseId,
    waitingConfirmedAt: now
  })
}

function evictOldestIdleClaudeSubagent(roster: ClaudeSubagentRoster): boolean {
  let oldestId: string | null = null
  let oldestStartedAt = Infinity
  for (const [id, tracked] of roster) {
    if (tracked.state === 'idle' && tracked.startedAt < oldestStartedAt) {
      oldestId = id
      oldestStartedAt = tracked.startedAt
    }
  }
  if (oldestId === null) {
    return false
  }
  roster.delete(oldestId)
  return true
}

/** SubagentStop. A one-shot child is finished — the row leaves immediately.
 *  A teammate-shaped id is only ending a TURN on claude 2.1.21x (the teammate
 *  stays alive awaiting mail), so its row flips to idle instead — unless a
 *  fold proved the id is really a workflow lane (listedAsSubagentTask), whose
 *  stop is a true finish. */
export function stopClaudeSubagent(roster: ClaudeSubagentRoster, id: string): void {
  const tracked = roster.get(id)
  if (!tracked) {
    return
  }
  if (!isClaudeTeammateLifecycleId(id) || tracked.listedAsSubagentTask === true) {
    roster.delete(id)
    return
  }
  tracked.backgroundTasksAuthoritative = undefined
  tracked.restoredFromSnapshot = undefined
  tracked.waitingToolName = undefined
  tracked.waitingToolInput = undefined
  tracked.waitingToolUseId = undefined
  tracked.waitingConfirmedAt = undefined
  tracked.state = 'idle'
}

/** Drop restored rows that no current-runtime child activity has confirmed. */
export function reapUnconfirmedRestoredClaudeSubagents(roster: ClaudeSubagentRoster): boolean {
  let changed = false
  for (const [id, tracked] of roster) {
    if (tracked.restoredFromSnapshot === true) {
      roster.delete(id)
      changed = true
    }
  }
  return changed
}

export function claudeRosterHasRestoredSnapshotSubagent(
  roster: ClaudeSubagentRoster | undefined
): boolean {
  if (!roster) {
    return false
  }
  for (const tracked of roster.values()) {
    if (tracked.restoredFromSnapshot === true) {
      return true
    }
  }
  return false
}

/** Whether a lifecycle agent id belongs to the named teammate. Teammate ids
 *  embed the name as `a<name>-<hex>`; requiring a hyphen-free suffix keeps
 *  teammate "rev" from matching "rev-two"'s ids (`arev-two-<hex>`), while a
 *  hyphenated name still matches its own ids exactly. */
export function claudeTeammateIdMatchesName(id: string, name: string): boolean {
  const prefix = `a${name}-`
  return id.startsWith(prefix) && !id.slice(prefix.length).includes('-')
}

/** Flip a teammate's rows to idle from a TeammateIdle hook, which is keyed by
 *  name. On claude 2.1.21x idle means "turn over, awaiting mail" — the
 *  teammate is alive and resumable, so the row stays (as idle) and is marked
 *  confirmedTeammate so lead-Stop folds keep it. Named teammates embed their
 *  name in `agent_id` (`a<name>-<hex>`), which is the only unambiguous
 *  mapping. Agent types are independent of teammate names, so a type fallback
 *  could idle unrelated live work when the teammate's start hook was lost. */
export function idleClaudeTeammateByName(roster: ClaudeSubagentRoster, name: string): boolean {
  let changed = false
  for (const [id, tracked] of roster) {
    if (claudeTeammateIdMatchesName(id, name)) {
      changed = changed || tracked.state !== 'idle' || tracked.confirmedTeammate !== true
      tracked.backgroundTasksAuthoritative = undefined
      tracked.restoredFromSnapshot = undefined
      // Why: leaving 'waiting' (if it was) — matches every other exit path's
      // contract that these fields are cleared once a row is no longer waiting.
      tracked.waitingToolName = undefined
      tracked.waitingToolInput = undefined
      tracked.waitingToolUseId = undefined
      tracked.waitingConfirmedAt = undefined
      tracked.state = 'idle'
      tracked.confirmedTeammate = true
    }
  }
  return changed
}

/** Only WORKING children gate the pane 'working' — idle teammates are
 *  alive-but-parked and must not pin a finished pane's spinner (#8825). */
export function claudeRosterHasWorkingSubagent(roster: ClaudeSubagentRoster | undefined): boolean {
  if (!roster) {
    return false
  }
  for (const tracked of roster.values()) {
    if (tracked.state === 'working') {
      return true
    }
  }
  return false
}

/** Whether any child's own PermissionRequest/AskUserQuestion is unresolved — mirrors
 *  claudeRosterHasWorkingSubagent. Any 'waiting' row must pin the pane 'waiting'
 *  regardless of the lead's own state, exactly as Codex's codexRosterEffectiveState
 *  scans its roster for any 'waiting' entry unconditionally (#P1: a second sibling's
 *  wait/resolution must never mask a first sibling's still-pending one). */
export function claudeRosterHasWaitingSubagent(roster: ClaudeSubagentRoster | undefined): boolean {
  return claudeRosterFindWaitingSubagent(roster) !== undefined
}

/** First roster entry still blocked on its own PermissionRequest/AskUserQuestion, if
 *  any — used to repoint the pane's single-slot wait pointer/tool-snapshot cache at a
 *  still-pending sibling when another child's wait resolves. */
export function claudeRosterFindWaitingSubagent(
  roster: ClaudeSubagentRoster | undefined
): [id: string, tracked: TrackedClaudeSubagent] | undefined {
  if (!roster) {
    return undefined
  }
  for (const entry of roster) {
    if (entry[1].state === 'waiting') {
      return entry
    }
  }
  return undefined
}

/** A working child observed in this listener runtime, not merely restored from disk. */
export function claudeRosterHasRuntimeWorkingSubagent(
  roster: ClaudeSubagentRoster | undefined
): boolean {
  if (!roster) {
    return false
  }
  for (const tracked of roster.values()) {
    if (tracked.state === 'working' && tracked.restoredFromSnapshot !== true) {
      return true
    }
  }
  return false
}

export function claudeRosterToSnapshots(
  roster: ClaudeSubagentRoster | undefined
): AgentSubagentSnapshot[] | undefined {
  if (!roster || roster.size === 0) {
    return undefined
  }
  const snapshots: AgentSubagentSnapshot[] = []
  for (const [id, tracked] of roster) {
    snapshots.push({
      id,
      state: tracked.state,
      startedAt: tracked.startedAt,
      agentType: tracked.agentType,
      description: tracked.description
    })
  }
  // Why: hook arrival order is not stable across reconciles; sort so equal
  // rosters serialize identically and downstream equality checks can dedupe.
  snapshots.sort((a, b) => a.startedAt - b.startedAt || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  return snapshots
}
