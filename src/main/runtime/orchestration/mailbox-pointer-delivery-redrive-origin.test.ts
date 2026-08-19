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
function buildHarness(overrides?: {
  writePty?: (ptyId: string, data: string) => boolean | Promise<boolean>
}) {
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
  const writePty = vi.fn(overrides?.writePty ?? (() => true))
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
  return { pointerDelivery, writePty, redriveMailbox, leaf, db }
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

  it('rolls back the pending write if the pty retires before a watermark ever exists', async () => {
    let resolveWrite!: (accepted: boolean) => void
    const pendingWrite = new Promise<boolean>((resolve) => {
      resolveWrite = resolve
    })
    const { pointerDelivery, redriveMailbox, leaf, db } = buildHarness({
      writePty: () => pendingWrite
    })

    // The PTY write is still in flight - finishPointerWrite (and thus setWatermark and
    // db.markAsDelivered) has not run yet, so only the flight itself, not a watermark or any
    // delivered-but-unrolled-back message id, knows about this reservation.
    pointerDelivery.deliver(leaf, { mailboxHandle: MAILBOX_HANDLE, origin: 'idle-transition' })

    pointerDelivery.retirePty(PTY_ID)
    await Promise.resolve()

    // Nothing was ever marked delivered, so there is nothing to roll back - but the mailbox
    // must still be redriven, or it is stranded forever behind a flight that will never settle.
    expect(db.markAsUndelivered).not.toHaveBeenCalled()
    expect(redriveMailbox).toHaveBeenCalledWith(MAILBOX_HANDLE, undefined, 'idle-transition')

    // Let the stale write settle after retirement without throwing.
    resolveWrite(true)
    await Promise.resolve()
  })

  it('rolls back a deactivated watermark (submission settled without ever sending Enter)', async () => {
    vi.useFakeTimers()
    try {
      const { pointerDelivery, writePty, redriveMailbox, leaf, db } = buildHarness()

      pointerDelivery.deliver(leaf, { mailboxHandle: MAILBOX_HANDLE, origin: 'idle-transition' })
      expect(writePty).toHaveBeenCalledTimes(1)

      // Before the 500ms auto-submit timer fires, the agent stops being idle - submission
      // settles via deactivateWatermark (not clearWatermark), and the flight itself is
      // already gone (settled) by the time this pty later retires.
      leaf.lastAgentStatus = 'working'
      await vi.advanceTimersByTimeAsync(500)
      // The auto-submit path never wrote '\r' - only the original pointer write counts.
      expect(writePty).toHaveBeenCalledTimes(1)

      pointerDelivery.retirePty(PTY_ID)
      await Promise.resolve()

      // Without the watermark itself carrying stagedMessageIds, this delivered-but-never-
      // submitted message would stay marked 'delivered' forever once its pty is gone.
      expect(db.markAsUndelivered).toHaveBeenCalledWith(['m1'])
      expect(redriveMailbox).toHaveBeenCalledWith(MAILBOX_HANDLE, undefined, 'idle-transition')
    } finally {
      vi.useRealTimers()
    }
  })

  it('coalesces a mailbox reserved through both its flight/watermark and a separate parked entry into one redrive', async () => {
    const { pointerDelivery, writePty, redriveMailbox, leaf } = buildHarness()

    // First delivery becomes the flight owner and (synchronously, in this harness) the
    // watermark owner too, reserving 'type_a'.
    pointerDelivery.deliver(leaf, {
      mailboxHandle: MAILBOX_HANDLE,
      origin: 'idle-transition',
      reservedTypes: new Set(['type_a'])
    })
    expect(writePty).toHaveBeenCalledTimes(1)

    // A second request for the SAME mailbox arrives while the first's flight is still
    // in-flight (delayed by its auto-submit timer): deliver() parks it separately, reserving
    // 'type_b', instead of re-watermarking.
    pointerDelivery.deliver(leaf, {
      mailboxHandle: MAILBOX_HANDLE,
      origin: 'idle-transition',
      reservedTypes: new Set(['type_b'])
    })
    expect(writePty).toHaveBeenCalledTimes(1)

    pointerDelivery.retirePty(PTY_ID)
    await Promise.resolve()

    // Retirement must redrive this mailbox exactly once, with the union of every source's
    // reservation - not once per source (double-delivery) and not with only one side's types.
    expect(redriveMailbox).toHaveBeenCalledTimes(1)
    expect(redriveMailbox).toHaveBeenCalledWith(
      MAILBOX_HANDLE,
      new Set(['type_a', 'type_b']),
      'idle-transition'
    )
  })
})
