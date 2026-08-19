// Distinguishes which call path is asking to push a mailbox pointer. An idle-transition
// push (a worker just went idle) may auto-push new dispatch:-addressed task mail, since
// that is new work being handed to an idle worker. The notifyMessageArrived fallback push
// (mail just arrived/was routed while status is unchanged) must NOT auto-push dispatch:
// mail — an existing detached-routing contract requires that mail wait for an explicit
// `check` instead of an unpinned pointer landing mid-turn. run: mailboxes are unaffected
// and stay deliverable from either origin.
export type OrchestrationMailboxDeliveryOrigin = 'idle-transition' | 'notification'

export function isMailboxHandleDeliverableForOrigin(
  mailboxHandle: string,
  origin: OrchestrationMailboxDeliveryOrigin
): boolean {
  if (mailboxHandle.startsWith('run:')) {
    return true
  }
  return origin === 'idle-transition' && mailboxHandle.startsWith('dispatch:')
}
