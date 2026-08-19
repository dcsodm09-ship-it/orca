import { describe, expect, it, vi } from 'vitest'
import type { OrchestrationDb } from './db'
import { OrchestrationMailboxDeliveryTarget } from './mailbox-delivery-target'
import { OrchestrationMailboxOwner, type OrchestrationMailboxLeaf } from './mailbox-owner'
import {
  OrchestrationMailboxPointerDelivery,
  type OrchestrationMailboxDeliveryOrigin,
  type OrchestrationMessageWaiter
} from './mailbox-pointer-delivery'
import { isMailboxHandleDeliverableForOrigin } from './mailbox-pointer-delivery-origin'

const TAB_ID = 'tab_redrive'
const LEAF_ID = 'leaf_redrive'
const PANE_KEY = `${TAB_ID}:${LEAF_ID}`
const PTY_ID = 'pty_redrive'
const TERMINAL_HANDLE = 'term_redrive_worker'
const DISPATCH_ID = 'dispatch_redrive'
const MAILBOX_HANDLE = `dispatch:${DISPATCH_ID}`

// Minimal fake covering only the OrchestrationDb surface these delivery paths touch.
function buildFakeDb(messages: { id: string; type: string; sequence: number }[]): OrchestrationDb {
  return {
    getCurrentRunForPane: () => undefined,
    getActiveDispatchForIdentity: () => ({ id: DISPATCH_ID, run_id: 'run_redrive' }),
    hasUndeliveredDirectMessageForRun: () => false,
    findActiveRemoteAttachmentForPane: () => undefined,
    getDispatchContextById: () => ({
      assignee_pane_key: PANE_KEY,
      assignee_handle: TERMINAL_HANDLE
    }),
    getUndeliveredUnreadMessages: () => messages,
    areUnreadMessages: () => true,
    markAsDelivered: vi.fn(),
    markAsUndelivered: vi.fn()
  } as unknown as OrchestrationDb
}

// Builds a pointer-delivery instance whose redriveMailbox dependency loops straight back
// into deliverForHandle, mirroring the orca-runtime wiring (redriveMailbox -> deliverPendingMessagesForHandle -> deliverForHandle).
function buildHarness() {
  const leaf: OrchestrationMailboxLeaf = {
    tabId: TAB_ID,
    leafId: LEAF_ID,
    ptyId: PTY_ID,
    writable: true,
    lastAgentStatus: 'idle',
    lastAgentStatusObservedLive: true,
    lastOscTitle: null,
    paneTitle: null
  }
  const writePty = vi.fn(() => true)
  const db = buildFakeDb([{ id: 'm1', type: 'status', sequence: 1 }])
  const mailboxOwner = new OrchestrationMailboxOwner({
    getDb: () => db,
    getLeaf: () => leaf,
    getLeafKey: (tabId, leafId) => `${tabId}:${leafId}`,
    getTerminalHandleForLeafKey: () => TERMINAL_HANDLE,
    getTerminalProcessIncarnation: () => null,
    onRoutedMessageTypes: () => {},
    onForeignMailboxRouted: () => {}
  })
  const deliveryTarget = new OrchestrationMailboxDeliveryTarget({
    getDb: () => db,
    getTerminalHandleForPaneKey: (paneKey) => (paneKey === PANE_KEY ? TERMINAL_HANDLE : null),
    hasTerminalHandle: (handle) => handle === TERMINAL_HANDLE,
    canProbePtyLiveness: () => false,
    controllerKnowsPtyIsLive: () => true,
    isLeafPtyProvenAbsent: async () => false
  })
  let pointerDelivery!: OrchestrationMailboxPointerDelivery<OrchestrationMessageWaiter>
  const redriveMailbox = vi.fn(
    (
      mailboxHandle: string,
      reservedTypes?: ReadonlySet<string>,
      origin?: OrchestrationMailboxDeliveryOrigin
    ) => pointerDelivery.deliverForHandle(mailboxHandle, reservedTypes, origin)
  )
  pointerDelivery = new OrchestrationMailboxPointerDelivery<OrchestrationMessageWaiter>({
    mailboxOwner,
    deliveryTarget,
    getDb: () => db,
    getLeaf: () => leaf,
    getLeafKey: (tabId, leafId) => `${tabId}:${leafId}`,
    getLiveLeafForHandle: () => leaf,
    getMessageWaiters: () => undefined,
    getTabTitle: () => null,
    getTerminalHandleForLeafKey: () => TERMINAL_HANDLE,
    isLeafPtyProvenAbsent: async () => false,
    redriveMailbox,
    writePty
  })
  return { pointerDelivery, writePty, redriveMailbox, leaf }
}

describe('mailbox redrive origin', () => {
  it('redrives a dispatch: mailbox interrupted mid-delivery instead of stranding it', async () => {
    const { pointerDelivery, writePty, redriveMailbox, leaf } = buildHarness()

    // Initial idle-transition push writes the pointer and arms the auto-submit flight.
    pointerDelivery.deliver(leaf, { mailboxHandle: MAILBOX_HANDLE, origin: 'idle-transition' })
    expect(writePty).toHaveBeenCalledTimes(1)

    // The worker's pty dies before the auto-submit timer fires: retirePty clears the
    // watermark and re-enters via the internal redrive() path.
    pointerDelivery.retirePty(PTY_ID)
    await Promise.resolve()

    // redrive() must pass 'idle-transition', not the default 'notification', or the
    // origin gate silently rejects dispatch: mail and strands it (the P2 bug).
    expect(redriveMailbox).toHaveBeenCalledWith(MAILBOX_HANDLE, undefined, 'idle-transition')
    expect(writePty).toHaveBeenCalledTimes(2)
  })

  it('control: a plain notification-origin redelivery attempt leaves dispatch: mail parked', () => {
    const { pointerDelivery, writePty, leaf } = buildHarness()

    pointerDelivery.deliver(leaf, { mailboxHandle: MAILBOX_HANDLE, origin: 'idle-transition' })
    pointerDelivery.retirePty(PTY_ID)

    // Simulates the pre-fix hardcoded redrive() call site.
    pointerDelivery.deliverForHandle(MAILBOX_HANDLE, undefined, 'notification')

    expect(writePty).toHaveBeenCalledTimes(1)
  })

  it('redrives a mailbox merely parked behind another in-flight write, not just the watermark owner', async () => {
    const { pointerDelivery, writePty, redriveMailbox, leaf } = buildHarness()
    const PARKED_MAILBOX_HANDLE = `dispatch:${DISPATCH_ID}_2`

    // First mailbox starts the flight and holds the watermark.
    pointerDelivery.deliver(leaf, { mailboxHandle: MAILBOX_HANDLE, origin: 'idle-transition' })
    expect(writePty).toHaveBeenCalledTimes(1)

    // Second mailbox arrives while the PTY is still in-flight for the first: it gets
    // parked (deliver()'s hasFlight(ptyId) branch), never watermarked, never written.
    pointerDelivery.deliver(leaf, {
      mailboxHandle: PARKED_MAILBOX_HANDLE,
      origin: 'idle-transition'
    })
    expect(writePty).toHaveBeenCalledTimes(1)

    // The worker's pty dies before either settles: retirePty used to only redrive the
    // watermarked mailbox and silently drop the merely-parked one along with the deleted map.
    pointerDelivery.retirePty(PTY_ID)
    await Promise.resolve()

    // Both mailboxes must be redriven, not just the one that happened to hold the watermark -
    // this is the fix: retirePty used to only surface releasedMailboxes (watermark owners) and
    // silently drop everything else parked behind the same PTY.
    expect(redriveMailbox).toHaveBeenCalledWith(MAILBOX_HANDLE, undefined, 'idle-transition')
    expect(redriveMailbox).toHaveBeenCalledWith(PARKED_MAILBOX_HANDLE, undefined, 'idle-transition')
  })

  it('gates dispatch: mailboxes to idle-transition origin, run: mailboxes to either', () => {
    expect(isMailboxHandleDeliverableForOrigin(MAILBOX_HANDLE, 'idle-transition')).toBe(true)
    expect(isMailboxHandleDeliverableForOrigin(MAILBOX_HANDLE, 'notification')).toBe(false)
    expect(isMailboxHandleDeliverableForOrigin('run:run_redrive', 'idle-transition')).toBe(true)
    expect(isMailboxHandleDeliverableForOrigin('run:run_redrive', 'notification')).toBe(true)
  })
})
