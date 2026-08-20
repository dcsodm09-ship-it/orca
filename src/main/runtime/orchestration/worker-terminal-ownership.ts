import type { WorkerDispatchState } from './types'

export type WorkerTerminalOwnershipState =
  | 'owned'
  | 'transferred'
  | 'user_owned'
  | 'external'
  | 'released'

export type WorkerTerminalReleaseState =
  | 'not_requested'
  | 'retained'
  | 'requested'
  | 'releasing'
  | 'released'
  | 'unknown'

export type WorkerTerminalRetainedReason =
  | 'external_terminal'
  | 'ownership_transferred'
  | 'user_takeover'
  | 'user_requested'
  | 'no_owned_resource'
  | 'identity_unproven'
  | 'legacy_ambiguous'
  | 'federation_unsupported'

export type WorkerTerminalArchiveStatus = 'captured' | 'empty' | 'unavailable'

export type WorkerTerminalResourceRow = {
  id: string
  origin_dispatch_id: string
  owner_dispatch_id: string
  prior_owner_dispatch_ids: string
  worktree_id: string | null
  terminal_handle: string
  pane_key: string | null
  process_incarnation: string | null
  host_scope: string | null
  ownership_state: WorkerTerminalOwnershipState
  release_state: WorkerTerminalReleaseState
  retained_reason: string | null
  release_requested_at: string | null
  release_completed_at: string | null
  release_error: string | null
  archive_source: string | null
  archive_status: WorkerTerminalArchiveStatus | null
  created_at: string
  updated_at: string
}

// Terminal state exposed by worker-list; process accounting, never Task/Dispatch outcome.
export type WorkerTerminalListState =
  | 'active'
  | 'reclaimable'
  | 'retained'
  | 'release_pending'
  | 'release_unknown'
  | 'released'

export type WorkerDispatchListState = WorkerDispatchState | 'unsupervised'

export type WorkerTerminalArchiveRow = {
  dispatch_id: string
  resource_id: string
  kind: 'transcript_pin' | 'terminal_tail'
  content: string
  created_at: string
}

export const WORKER_SETTLED_STATES: readonly WorkerDispatchState[] = [
  'succeeded',
  'failed',
  'stopped',
  'abandoned'
]

export const WORKER_RELEASABLE_STATES: readonly WorkerDispatchState[] = ['succeeded', 'failed']

// Process accounting for worker-list; deliberately independent of Task/Dispatch outcome.
export function deriveWorkerTerminalListState(params: {
  workerState: WorkerDispatchListState
  agentTerminalHandle: string | null
  resource: WorkerTerminalResourceRow | null
}): WorkerTerminalListState | null {
  const { resource } = params
  if (!resource) {
    return params.agentTerminalHandle ? 'retained' : null
  }
  if (resource.release_state === 'released') {
    return 'released'
  }
  if (resource.release_state === 'unknown') {
    return 'release_unknown'
  }
  if (resource.release_state === 'requested' || resource.release_state === 'releasing') {
    return 'release_pending'
  }
  if (resource.ownership_state !== 'owned' || resource.release_state === 'retained') {
    return 'retained'
  }
  if (
    params.workerState !== 'unsupervised' &&
    WORKER_RELEASABLE_STATES.includes(params.workerState)
  ) {
    return 'reclaimable'
  }
  return params.workerState !== 'unsupervised' && WORKER_SETTLED_STATES.includes(params.workerState)
    ? 'retained'
    : 'active'
}

// Projects a dispatch's frozen start_options JSON into worker-list's agent/model columns.
const EMPTY_WORKER_LIST_LAUNCH_SELECTION = { agent: null, model: null } as const

/** Why: unlike exposeWorker's equivalent unguarded JSON.parse (which only fails the ONE
 *  dispatch being queried by worker-show), this feeds a worker-list row map — one malformed
 *  or non-object start_options value (e.g. literal "null", matching JSON.stringify(null))
 *  would otherwise throw uncaught through the whole query and blank the entire list, not just
 *  the offending row. Not reachable from either current insert path (both pass object
 *  literals), but createStartingWorkerDispatch's startOptions param is typed `unknown` with no
 *  validation, so this is defense against a real latent input shape, not a hypothetical one. */
export function deriveWorkerListLaunchSelection(startOptionsJson: string | null): {
  agent: string | null
  model: string | null
} {
  if (!startOptionsJson) {
    return EMPTY_WORKER_LIST_LAUNCH_SELECTION
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(startOptionsJson)
  } catch {
    return EMPTY_WORKER_LIST_LAUNCH_SELECTION
  }
  if (typeof parsed !== 'object' || parsed === null) {
    return EMPTY_WORKER_LIST_LAUNCH_SELECTION
  }
  const receipt = parsed as {
    agent?: string | null
    launch?: {
      effective?: { model?: string | null } | null
      requested?: { model?: string | null }
    }
  }
  const rawModel = receipt.launch?.effective?.model ?? receipt.launch?.requested?.model ?? null
  return {
    agent: typeof receipt.agent === 'string' ? receipt.agent : null,
    model: typeof rawModel === 'string' ? rawModel : null
  }
}
