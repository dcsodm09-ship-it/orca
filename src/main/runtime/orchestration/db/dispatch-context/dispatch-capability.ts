import { randomBytes, timingSafeEqual } from 'node:crypto'
import { OrchestrationError } from '../../orchestration-error'
import { parsePaneKey } from '../../../../../shared/stable-pane-id'
import { hashDispatchCapability } from '../dispatch-capability-hash'
import {
  DISPATCH_PANE_KEY_MATCH_SUFFIX_SQL,
  isEquivalentPaneKey,
  paneKeyMatchSuffix
} from '../pane-key-match'
import type { OrchestrationDb } from '../orchestration-db'

export function mintDispatchCapability(
  this: OrchestrationDb,
  params: {
    dispatchId: string
    paneKey: string
    processIncarnation: string
  }
): string {
  const dispatch = this.getDispatchContextById(params.dispatchId)
  if (!dispatch || (dispatch.status !== 'pending' && dispatch.status !== 'dispatched')) {
    throw new OrchestrationError(
      'dispatch_inactive',
      `Dispatch ${params.dispatchId} is not active.`
    )
  }
  const capability = `dcap_${randomBytes(32).toString('base64url')}`
  const paneSuffix = parsePaneKey(params.paneKey) ? paneKeyMatchSuffix(params.paneKey) : null
  // Why (#14809): the caller's reuse lookup and this mint straddle an `await`, so a concurrent
  // --inject retry can find the same capability-less context. Gate the write on capability_hash
  // still being NULL so only the first writer wins the single-statement race; the loser's 0-row
  // UPDATE throws instead of silently re-minting over (and double-injecting into) the winner.
  // Also re-check pane occupancy here, not just at the caller's earlier read: a second, independent
  // dispatch can legitimately claim this pane in the window between that read and this rebind, since
  // the rebind never re-validated it — mirrors the NOT EXISTS guard DISPATCH_CONTEXT_CLAIM_SQL
  // already applies at create time.
  // Why (dispatch-authority race): the earlier `getDispatchContextById` read above is a separate,
  // un-transacted statement from this UPDATE (unlike prepareStartingWorkerAuthority's own inline
  // mint, which wraps its read+write in BEGIN IMMEDIATE), so a concurrent writer on a genuinely
  // separate connection - a second Orca instance sharing this DB file via ORCA_BYPASS_SINGLE_
  // INSTANCE_LOCK/dev mode, or a network-mounted userData dir; app.requestSingleInstanceLock()
  // rules this out for a normal packaged install - could flip `status` between the two, e.g.
  // failDispatch() marking this exact context 'failed'. Re-check status IN the same UPDATE, not
  // just at the read above; must allow the same two statuses as the initial guard ('pending' is
  // reachable via this function too - not every 'pending' dispatch takes the composed-worker
  // path's own separate mint - 'dispatched' covers the plain --inject path).
  const result = this.db
    .prepare(
      `UPDATE dispatch_contexts
       SET capability_hash = ?, assignee_pane_key = ?, process_incarnation = ?,
           capability_revoked_at = NULL
       WHERE id = ? AND capability_hash IS NULL
         AND status IN ('pending', 'dispatched')
         AND NOT EXISTS (
           SELECT 1 FROM dispatch_contexts active
           WHERE active.id != ?
             AND active.status IN ('pending', 'dispatched')
             AND (
               active.assignee_pane_key = ?
               OR (
                 ? IS NOT NULL
                 AND active.assignee_pane_key IS NOT NULL
                 AND instr(active.assignee_pane_key, ':') > 1
                 AND ${DISPATCH_PANE_KEY_MATCH_SUFFIX_SQL} = ?
               )
             )
         )`
    )
    .run(
      hashDispatchCapability(capability),
      params.paneKey,
      params.processIncarnation,
      params.dispatchId,
      params.dispatchId,
      params.paneKey,
      paneSuffix,
      paneSuffix
    )
  if (result.changes === 0) {
    // Why: disambiguate the three 0-row causes, status first — a concurrent mint already claimed
    // this row's capability, a concurrent dispatch claimed this row's target pane out from under
    // it, or (dispatch-authority race) the row left 'pending'/'dispatched' entirely between the
    // read above and this UPDATE (e.g. a concurrent failDispatch() call on this exact context).
    const current = this.getDispatchContextById(params.dispatchId)
    if (!current || (current.status !== 'pending' && current.status !== 'dispatched')) {
      throw new OrchestrationError(
        'dispatch_inactive',
        `Dispatch ${params.dispatchId} is not active.`
      )
    }
    if (current.capability_hash) {
      throw new OrchestrationError(
        'dispatch_capability_already_minted',
        `Dispatch ${params.dispatchId} already has a lifecycle capability from a concurrent request.`
      )
    }
    throw new OrchestrationError(
      'dispatch_pane_reused',
      `Dispatch ${params.dispatchId}'s pane was claimed by another active Dispatch before this capability could bind.`
    )
  }
  return capability
}

export function verifyDispatchCapability(
  this: OrchestrationDb,
  params: {
    dispatchId: string
    capability: string | undefined
    paneKey: string | undefined
    processIncarnation: string | undefined
  }
): { valid: true } | { valid: false; reason: string } {
  const dispatch = this.getDispatchContextById(params.dispatchId)
  if (!dispatch) {
    return { valid: false, reason: `Dispatch ${params.dispatchId} was not found.` }
  }
  if (!dispatch.capability_hash) {
    return { valid: false, reason: `Dispatch ${params.dispatchId} has no lifecycle capability.` }
  }
  if (dispatch.capability_revoked_at) {
    return { valid: false, reason: `Dispatch ${params.dispatchId} capability is revoked.` }
  }
  if (!params.capability) {
    return { valid: false, reason: 'The Dispatch capability is missing.' }
  }
  const expected = Buffer.from(dispatch.capability_hash, 'hex')
  const observed = Buffer.from(hashDispatchCapability(params.capability), 'hex')
  if (expected.length !== observed.length || !timingSafeEqual(expected, observed)) {
    return { valid: false, reason: 'The Dispatch capability is invalid.' }
  }
  if (
    !dispatch.assignee_pane_key ||
    !params.paneKey ||
    !isEquivalentPaneKey(dispatch.assignee_pane_key, params.paneKey)
  ) {
    return { valid: false, reason: 'The caller is not the Dispatch pane.' }
  }
  if (
    !dispatch.process_incarnation ||
    !params.processIncarnation ||
    dispatch.process_incarnation !== params.processIncarnation
  ) {
    return { valid: false, reason: 'The Dispatch process incarnation changed.' }
  }
  return { valid: true }
}

export function revokeDispatchCapability(this: OrchestrationDb, dispatchId: string): void {
  this.db
    .prepare(
      `UPDATE dispatch_contexts
       SET capability_revoked_at = COALESCE(capability_revoked_at, datetime('now'))
       WHERE id = ?`
    )
    .run(dispatchId)
}

export type DispatchCapabilityMethods = {
  mintDispatchCapability: typeof mintDispatchCapability
  verifyDispatchCapability: typeof verifyDispatchCapability
  revokeDispatchCapability: typeof revokeDispatchCapability
}

export function attachDispatchCapability(ctor: { prototype: object }): void {
  Object.assign(ctor.prototype, {
    mintDispatchCapability,
    verifyDispatchCapability,
    revokeDispatchCapability
  })
}
