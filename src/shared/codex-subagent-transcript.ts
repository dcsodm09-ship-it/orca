import { extname, isAbsolute } from 'node:path'

import {
  finishCodexSubagent,
  isHookConfirmedCodexSubagent,
  setCodexSubagentModel,
  touchCodexSubagentConfirmedAt,
  upsertCodexSubagent,
  type CodexSubagentRoster
} from './codex-subagent-roster'
import {
  readJsonlCursor,
  record,
  resolveChildTranscript,
  SAFE_THREAD_ID,
  type JsonlCursor,
  type JsonRecord
} from './codex-transcript-jsonl-cursor'

// Why: retire a child whose rollout stays unreadable this long, else a deleted/never-written file pins a phantom row forever.
const CHILD_UNREADABLE_GRACE_MS = 60_000

type TrackedTranscriptSubagent = JsonlCursor & {
  description?: string
  /** Latest model seen in the child's own rollout. Retained across polls
   *  because the cursor is incremental: `turn_context` is emitted once per
   *  turn, so a later read usually carries no model at all. */
  model?: string
  startedAt: number
  unresolvedSince?: number
}

export type CodexSubagentTranscriptState = {
  parent: JsonlCursor
  subagents: Map<string, TrackedTranscriptSubagent>
}

function readActivity(recordValue: JsonRecord):
  | {
      id: string
      description?: string
      kind: 'started' | 'interacted' | 'interrupted'
      startedAt: number
    }
  | undefined {
  if (recordValue.type !== 'event_msg') {
    return undefined
  }
  const payload = record(recordValue.payload)
  if (payload?.type !== 'sub_agent_activity') {
    return undefined
  }
  const id = typeof payload.agent_thread_id === 'string' ? payload.agent_thread_id.trim() : ''
  const rawKind = typeof payload.kind === 'string' ? payload.kind.toLowerCase() : ''
  if (
    !SAFE_THREAD_ID.test(id) ||
    (rawKind !== 'started' && rawKind !== 'interacted' && rawKind !== 'interrupted')
  ) {
    return undefined
  }
  return {
    id,
    description:
      typeof payload.agent_path === 'string' ? payload.agent_path.trim() || undefined : undefined,
    kind: rawKind,
    startedAt:
      typeof payload.occurred_at_ms === 'number' && Number.isFinite(payload.occurred_at_ms)
        ? payload.occurred_at_ms
        : Date.now()
  }
}

/** Latest model from the child's own `turn_context` records. A child can be
 *  launched on a different model than its parent, so this is read from the
 *  child rollout rather than inherited. */
function readChildModel(records: JsonRecord[]): string | undefined {
  let model: string | undefined
  for (const recordValue of records) {
    if (recordValue.type !== 'turn_context') {
      continue
    }
    const payload = record(recordValue.payload)
    const value = typeof payload?.model === 'string' ? payload.model.trim() : ''
    if (value) {
      model = value
    }
  }
  return model
}

function childIsComplete(records: JsonRecord[]): boolean {
  let complete = false
  for (const recordValue of records) {
    if (recordValue.type !== 'event_msg') {
      continue
    }
    const payload = record(recordValue.payload)
    if (payload?.type === 'task_started') {
      complete = false
    } else if (payload?.type === 'task_complete') {
      complete = true
    }
  }
  return complete
}

export function createCodexSubagentTranscriptState(): CodexSubagentTranscriptState {
  return {
    parent: { offset: 0, carry: '' },
    subagents: new Map()
  }
}

export function hasTrackedCodexTranscriptSubagents(
  state: CodexSubagentTranscriptState | undefined
): boolean {
  return Boolean(state && state.subagents.size > 0)
}

export function reconcileCodexSubagentTranscript(
  state: CodexSubagentTranscriptState,
  roster: CodexSubagentRoster,
  transcriptPath: string | undefined
): void {
  const normalizedPath = transcriptPath?.trim()
  if (!normalizedPath || !isAbsolute(normalizedPath) || extname(normalizedPath) !== '.jsonl') {
    return
  }
  if (state.parent.filePath !== normalizedPath) {
    for (const id of state.subagents.keys()) {
      finishCodexSubagent(roster, id)
    }
    state.parent = { filePath: normalizedPath, offset: 0, carry: '' }
    state.subagents.clear()
  }
  for (const recordValue of readJsonlCursor(state.parent) ?? []) {
    const activity = readActivity(recordValue)
    if (!activity) {
      continue
    }
    if (activity.kind === 'interrupted') {
      finishCodexSubagent(roster, activity.id)
      state.subagents.delete(activity.id)
      continue
    }
    const tracked = state.subagents.get(activity.id) ?? {
      offset: 0,
      carry: '',
      startedAt: activity.startedAt
    }
    tracked.description = activity.description ?? tracked.description
    state.subagents.set(activity.id, tracked)
    upsertCodexSubagent(
      roster,
      activity.id,
      { description: tracked.description, state: 'working', source: 'transcript' },
      tracked.startedAt
    )
  }
  const entriesByDirectory = new Map<string, string[]>()
  const now = Date.now()
  for (const [id, tracked] of state.subagents) {
    if (!tracked.filePath) {
      tracked.filePath = resolveChildTranscript(
        normalizedPath,
        id,
        tracked.startedAt,
        entriesByDirectory
      )
    }
    const records = readJsonlCursor(tracked)
    if (!records) {
      // Why: a rollout that never appears (or is deleted) has no completion event, so time-box it instead of leaking a working row.
      tracked.filePath = undefined
      tracked.unresolvedSince ??= now
      if (now - tracked.unresolvedSince <= CHILD_UNREADABLE_GRACE_MS) {
        continue
      }
      // Why: this id's OWN transcript stayed unreadable past the grace window, but it may since
      // have been separately confirmed live by a hook (upgrading its roster row to
      // source:'hook') — that confirmation must not be silently undone just because transcript
      // polling itself found nothing. Same reasoning and guard as
      // retireExpiredUnresolvedCodexSubagentTranscripts; this is the OTHER call site the same
      // grace-expiry condition can reach (every reconcile, not just at Stop), so it needs the
      // identical guard, independently.
      if (!isHookConfirmedCodexSubagent(roster, id)) {
        finishCodexSubagent(roster, id)
      }
      state.subagents.delete(id)
      continue
    }
    tracked.unresolvedSince = undefined
    tracked.model = readChildModel(records) ?? tracked.model
    // Why: re-applied every reconcile, not just on discovery — the parent's
    // own activity upsert can rebuild this child's roster entry, which would
    // otherwise drop a model found on an earlier poll.
    setCodexSubagentModel(roster, id, tracked.model)
    // Why: only ACTUAL new content is treated as liveness evidence — not merely a successful,
    // unchanged read (readJsonlCursor returns `[]`, not undefined, when the file is readable but
    // hasn't grown since the last poll). A child whose rollout stops growing because the process
    // crashed right after writing task_started would otherwise look "alive" forever under the
    // 1s poll (every tick reads the same static file successfully), permanently defeating the
    // staleness bound this same mechanism exists to enforce. Genuinely new lines are real,
    // repeated proof of life for a child only ever reconfirmed via transcript reads.
    if (records.length > 0) {
      touchCodexSubagentConfirmedAt(roster, id)
    }
    if (!childIsComplete(records)) {
      continue
    }
    // Why: unlike the grace-expiry path above, task_complete is direct evidence from the
    // child's OWN rollout that it finished — this overrides hook-confirmed status (the hook's
    // own Stop may simply not have arrived yet), so this delete stays unconditional.
    finishCodexSubagent(roster, id)
    state.subagents.delete(id)
  }
}

/**
 * Force-check every currently-`unresolvedSince`-marked child against the grace deadline and
 * retire the expired ones, without needing a fresh rollout read or a later reconcile call.
 *
 * `reconcileCodexSubagentTranscript`'s own grace check only re-evaluates when reconcile runs
 * again — but Stop is normally the last hook event of a turn, so a child that first went
 * unresolved at (or shortly before) that same Stop's reconcile would have its deadline set to
 * "now" and never get a later call to re-check it against. Call this once at Stop, in addition
 * to (not instead of) reconcileCodexSubagentTranscript, so the grace window still expires on
 * its own. Only deletes the roster row when it is NOT hook-confirmed: an id can be tracked here
 * (transcript-discovered) and later separately confirmed by a live hook, upgrading its roster row
 * to 'source: hook' — deleting that row on this path would silently undo a real hook
 * confirmation and could kill a genuinely still-running child. A hook-confirmed id is instead the
 * Stop-time hook veto's responsibility (see hasHookConfirmedCodexSubagent and its own bounded
 * fallback, retireStaleHookConfirmedCodexSubagents) — this function still stops TRACKING it here
 * either way, since transcript-side polling has nothing further to add once a hook owns the id.
 */
export function retireExpiredUnresolvedCodexSubagentTranscripts(
  state: CodexSubagentTranscriptState,
  roster: CodexSubagentRoster,
  now: number
): void {
  for (const [id, tracked] of state.subagents) {
    if (tracked.unresolvedSince === undefined) {
      continue
    }
    if (now - tracked.unresolvedSince <= CHILD_UNREADABLE_GRACE_MS) {
      continue
    }
    if (!isHookConfirmedCodexSubagent(roster, id)) {
      finishCodexSubagent(roster, id)
    }
    state.subagents.delete(id)
  }
}
