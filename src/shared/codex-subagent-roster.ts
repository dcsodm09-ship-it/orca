import {
  AGENT_MODEL_MAX_LENGTH,
  AGENT_STATUS_MAX_SUBAGENTS,
  AGENT_STATUS_STALE_AFTER_MS,
  AGENT_STATUS_TOOL_INPUT_MAX_LENGTH,
  AGENT_TYPE_MAX_LENGTH,
  type AgentSubagentSnapshot
} from './agent-status-types'
import { normalizeOptionalField } from './agent-status-field-normalization'

const CODEX_SUBAGENT_ID_MAX_LENGTH = 64

export type CodexSubagentRoster = Map<string, TrackedCodexSubagent>

type TrackedCodexSubagent = {
  agentType?: string
  description?: string
  model?: string
  state: 'working' | 'waiting'
  startedAt: number
  /** Provenance of this row: 'hook' means a live SubagentStart/SubagentStop or an
   *  agent_id-carrying PreToolUse/PostToolUse/PermissionRequest confirmed it directly;
   *  'transcript' means only rollout-file polling (reconcileCodexSubagentTranscript) has
   *  seen it. Only 'hook' rows must survive the lead's own Stop — a 'transcript' row is
   *  independently retired by its own grace timeout instead. Upgrade-only: once a row is
   *  hook-confirmed it must never be demoted back to 'transcript' by a later transcript
   *  poll of the same id, or Stop-time protection would silently disappear underneath it.
   */
  source: 'hook' | 'transcript'
  /** The row was rebuilt from a persisted snapshot at Orca restart and no live
   *  hook has confirmed it since, so the only thing backing it is a claim
   *  written by an agent process that may no longer exist. Mirrors
   *  TrackedClaudeSubagent.restoredFromSnapshot — lets a liveness check reap
   *  it when the pane's local process is gone. Cleared by any hook-confirmed
   *  ('source: hook') touch of the id; a transcript-only touch does not clear
   *  it, since rollout polling alone is not trusted proof of current liveness. */
  restoredFromSnapshot?: true
  /** Wall-clock time of the most recent upsert touch (hook or transcript). A
   *  'hook' row is otherwise exempt from the Stop-time roster wipe entirely
   *  (see hasHookConfirmedCodexSubagent), so this is the only bounded fallback
   *  for a child whose own SubagentStop hook was lost (a known Codex CLI gap)
   *  — without it such a row would survive forever. Deliberately wall-clock,
   *  not a per-Stop-call counter: AgentHookServer's scheduleCodexSubagentPoll
   *  re-normalizes the SAME saved hook body on a 1s timer whenever a transcript
   *  child is still unresolved, which would re-run the Stop branch (and any
   *  per-call counter in it) many times for what is really one real Stop —
   *  wall-clock elapsed time is unaffected by how many times that replay runs
   *  within the same short window. Updated by any upsert touch. */
  lastConfirmedAt: number
}

export function upsertCodexSubagent(
  roster: CodexSubagentRoster,
  id: string,
  fields: {
    agentType?: string
    description?: string
    model?: string
    state: 'working' | 'waiting'
    /** Defaults to 'hook' — every production caller except the transcript
     *  reconciler passes this explicitly; the default only backstops direct
     *  unit-test callers that don't care about provenance. */
    source?: 'hook' | 'transcript'
    /** Stamp a newly created row as hydrated-from-snapshot-unconfirmed. Only
     *  meaningful on creation — callers that seed a fresh roster from a
     *  restart snapshot pass this; a 'hook' touch of an existing row always
     *  clears the flag below regardless of this field. */
    restoredFromSnapshot?: true
  },
  now: number
): void {
  const normalizedId = id.trim()
  if (normalizedId.length === 0 || normalizedId.length > CODEX_SUBAGENT_ID_MAX_LENGTH) {
    return
  }
  const agentType = normalizeOptionalField(fields.agentType, AGENT_TYPE_MAX_LENGTH)
  const description = normalizeOptionalField(fields.description, AGENT_STATUS_TOOL_INPUT_MAX_LENGTH)
  const model = normalizeOptionalField(fields.model, AGENT_MODEL_MAX_LENGTH)
  const source = fields.source ?? 'hook'
  // Why: deliberately real wall-clock time, NOT the caller-supplied `now` — the transcript
  // reconciler passes the child's original discovery timestamp as `now` (for `startedAt`'s
  // sake), which never advances across repeated touches of an already-tracked id. Using it
  // here would freeze lastConfirmedAt at discovery time and make a row that's genuinely being
  // re-confirmed every reconcile cycle look increasingly stale anyway.
  const confirmedAt = Date.now()
  const existing = roster.get(normalizedId)
  if (existing) {
    existing.agentType = agentType ?? existing.agentType
    existing.description = description ?? existing.description
    existing.model = model ?? existing.model
    existing.state = fields.state
    existing.source = existing.source === 'hook' ? 'hook' : source
    // Why: any live touch (hook or transcript) is fresh evidence this child is
    // still alive, resetting the bounded stale-confirmation fallback below.
    existing.lastConfirmedAt = confirmedAt
    if (source === 'hook') {
      // Why: a live hook event proves the process behind a restored row is
      // still running it, so the liveness reap must stop treating it as a claim.
      existing.restoredFromSnapshot = undefined
    }
    return
  }
  if (roster.size >= AGENT_STATUS_MAX_SUBAGENTS) {
    return
  }
  roster.set(normalizedId, {
    agentType,
    description,
    model,
    state: fields.state,
    startedAt: now,
    source,
    lastConfirmedAt: confirmedAt,
    ...(fields.restoredFromSnapshot ? { restoredFromSnapshot: true } : {})
  })
}

export function finishCodexSubagent(roster: CodexSubagentRoster, id: string): void {
  roster.delete(id.trim())
}

/**
 * Record that an already-tracked child was just proven alive by direct evidence (its own
 * rollout file was successfully read AND produced new records since the last read — a bare
 * successful-but-unchanged read is NOT treated as evidence, since a child that crashed right
 * after writing task_started leaves exactly that shape and would otherwise look permanently
 * alive under a 1s poll) — narrower than upsertCodexSubagent for the same reason
 * setCodexSubagentModel is: it never creates a row and never touches state/model/source, only
 * lastConfirmedAt, so a touch cannot resurrect a finished child, move its lifecycle, or silently
 * downgrade a 'waiting' row to 'working'. Without this, a hook-confirmed row whose child is only
 * ever reconfirmed via genuinely new transcript activity (no further hook events) would still go
 * stale and be retired by retireStaleHookConfirmedCodexSubagents despite this direct, repeated
 * proof it's alive.
 */
export function touchCodexSubagentConfirmedAt(roster: CodexSubagentRoster, id: string): void {
  const existing = roster.get(id.trim())
  if (!existing) {
    return
  }
  existing.lastConfirmedAt = Date.now()
}

/**
 * Record the model a already-tracked child is running. Deliberately narrower
 * than `upsertCodexSubagent`: it never creates a roster entry and never touches
 * `state`, so late model discovery from a child rollout cannot resurrect a
 * finished child nor move any child's lifecycle.
 */
export function setCodexSubagentModel(
  roster: CodexSubagentRoster,
  id: string,
  model: string | undefined
): void {
  const normalizedModel = normalizeOptionalField(model, AGENT_MODEL_MAX_LENGTH)
  if (!normalizedModel) {
    return
  }
  const existing = roster.get(id.trim())
  if (!existing) {
    return
  }
  existing.model = normalizedModel
}

export function seedCodexSubagentRoster(
  roster: CodexSubagentRoster,
  snapshots: readonly AgentSubagentSnapshot[],
  options?: {
    /** True only for the Orca-restart disk hydrate path, where the writing
     *  process may already be gone by the time this runs — not for relay
     *  reconnect reseeding, whose owning pane is never local (see
     *  reconcileRemoteCodexState) and so is never a liveness-reap candidate. */
    restoredFromSnapshot?: boolean
  }
): void {
  for (const snapshot of snapshots) {
    if (snapshot.state !== 'working' && snapshot.state !== 'waiting') {
      continue
    }
    upsertCodexSubagent(
      roster,
      snapshot.id,
      {
        agentType: snapshot.agentType,
        description: snapshot.description,
        model: snapshot.model,
        state: snapshot.state,
        // Why: a restored/relay-reseeded snapshot is our own prior confirmed state, not a
        // fresh unconfirmed transcript guess — treat it as hook-confirmed so a reconnect
        // can't make an already-known child newly fragile against the next lead Stop.
        source: 'hook',
        ...(options?.restoredFromSnapshot ? { restoredFromSnapshot: true } : {})
      },
      snapshot.startedAt
    )
  }
}

/** Drop restored rows that no current-runtime hook activity has confirmed. */
export function reapUnconfirmedRestoredCodexSubagents(roster: CodexSubagentRoster): boolean {
  let changed = false
  for (const [id, tracked] of roster) {
    if (tracked.restoredFromSnapshot === true) {
      roster.delete(id)
      changed = true
    }
  }
  return changed
}

export function codexRosterHasRestoredSnapshotSubagent(
  roster: CodexSubagentRoster | undefined
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

/** Only WORKING children gate the pane 'working' — mirrors claudeRosterHasWorkingSubagent. */
export function codexRosterHasWorkingSubagent(roster: CodexSubagentRoster | undefined): boolean {
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

export function codexRosterToSnapshots(
  roster: CodexSubagentRoster | undefined
): AgentSubagentSnapshot[] | undefined {
  if (!roster || roster.size === 0) {
    return undefined
  }
  const snapshots = Array.from(roster, ([id, tracked]) => ({
    id,
    agentType: tracked.agentType,
    description: tracked.description,
    model: tracked.model,
    state: tracked.state,
    startedAt: tracked.startedAt
  }))
  snapshots.sort((a, b) => a.startedAt - b.startedAt || a.id.localeCompare(b.id))
  return snapshots
}

export function codexRosterEffectiveState(
  roster: CodexSubagentRoster | undefined,
  leadState: 'working' | 'waiting' | 'done'
): 'working' | 'waiting' | 'done' {
  if (!roster || roster.size === 0) {
    return leadState
  }
  for (const tracked of roster.values()) {
    if (tracked.state === 'waiting') {
      return 'waiting'
    }
  }
  return leadState === 'done' ? 'working' : leadState
}

/** Whether any row was confirmed by a live hook event (not merely transcript polling). A lead
 *  Stop must never wipe the roster while this is true, or a genuinely still-working child gets
 *  reported 'done' the instant its parent's own turn ends. */
export function hasHookConfirmedCodexSubagent(roster: CodexSubagentRoster | undefined): boolean {
  if (!roster) {
    return false
  }
  for (const tracked of roster.values()) {
    if (tracked.source === 'hook') {
      return true
    }
  }
  return false
}

/** Whether this specific id's roster row is hook-confirmed (as opposed to
 *  transcript-only or absent). Lets a transcript-scoped retirement path (see
 *  retireExpiredUnresolvedCodexSubagentTranscripts) defer to the Stop-time
 *  hook veto/bounded fallback below instead of deleting a row it doesn't own. */
export function isHookConfirmedCodexSubagent(
  roster: CodexSubagentRoster | undefined,
  id: string
): boolean {
  return roster?.get(id)?.source === 'hook'
}

// Why: reuses the same generous, already-established convention as AGENT_STATUS_STALE_AFTER_MS
// (the wire/mobile status projection's own "give up on a stale wait" bound) rather than a fresh
// magic number — long enough that a child merely quiet for a while isn't falsely reaped, but
// still bounded so a lost SubagentStop (a documented Codex CLI gap) can't pin a row forever.
const HOOK_CONFIRMED_SUBAGENT_STALE_MS = AGENT_STATUS_STALE_AFTER_MS

/** Delete every 'hook'-confirmed row that has gone unconfirmed (no fresh hook or transcript
 *  touch — see TrackedCodexSubagent.lastConfirmedAt) longer than HOOK_CONFIRMED_SUBAGENT_STALE_MS.
 *  Call once per lead Stop, only when the roster was NOT wholesale-wiped (i.e.
 *  hasHookConfirmedCodexSubagent blocked that wipe) — this is that veto's bounded fallback, not a
 *  replacement for it. Deliberately wall-clock rather than a per-call counter — see
 *  TrackedCodexSubagent.lastConfirmedAt for why a counter is unsafe here (poll replay). Returns
 *  the ids retired, for tests/logging. */
export function retireStaleHookConfirmedCodexSubagents(
  roster: CodexSubagentRoster,
  now: number
): string[] {
  const retired: string[] = []
  for (const [id, tracked] of roster) {
    if (tracked.source !== 'hook') {
      continue
    }
    if (now - tracked.lastConfirmedAt > HOOK_CONFIRMED_SUBAGENT_STALE_MS) {
      roster.delete(id)
      retired.push(id)
    }
  }
  return retired
}
