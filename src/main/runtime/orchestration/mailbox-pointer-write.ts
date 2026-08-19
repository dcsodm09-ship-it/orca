import { isCursorAgentTitle } from '../../../shared/agent-detection'
import type { OrchestrationDb } from './db'
import { formatMessagePointer } from './formatter'
import {
  shouldReleaseOrchestrationPointer,
  type OrchestrationMessageWaiter
} from './mailbox-pointer-eligibility'
import type { OrchestrationMailboxLeaf, OrchestrationMailboxOwner } from './mailbox-owner'
import type {
  OrchestrationMailboxPointerState,
  OrchestrationMailboxDeliveryFlight
} from './mailbox-pointer-state'
import { submitOrchestrationMailboxPointer } from './mailbox-pointer-submit'

export type PointerWriteDependencies<TWaiter extends OrchestrationMessageWaiter> = {
  mailboxOwner: OrchestrationMailboxOwner
  state: OrchestrationMailboxPointerState
  getDb: () => OrchestrationDb | null
  getLeaf: (leafKey: string) => OrchestrationMailboxLeaf | undefined
  getLeafKey: (tabId: string, leafId: string) => string
  getMessageWaiters: (mailboxHandle: string) => ReadonlySet<TWaiter> | undefined
  getTabTitle: (tabId: string) => string | null | undefined
  isLeafPtyProvenAbsent: (ptyId: string) => Promise<boolean>
  writePty: (ptyId: string, data: string) => boolean | Promise<boolean>
  settle: (ptyId: string, flight: OrchestrationMailboxDeliveryFlight) => void
  redrive: (mailboxHandle: string, force?: boolean) => void
}

// Why: writes the pointer to the PTY and finishes the flight (mark delivered + schedule
// the auto-submit, or clear the watermark for a cursor-agent title that can't auto-submit).
// Extracted from OrchestrationMailboxPointerDelivery to keep that file under the max-lines
// budget; takes a deps bag rather than `this` so it stays a plain, testable function.
export function stageOrchestrationMailboxPointer<TWaiter extends OrchestrationMessageWaiter>(
  deps: PointerWriteDependencies<TWaiter>,
  leaf: OrchestrationMailboxLeaf,
  mailboxHandle: string,
  unread: readonly { id: string; type: string; sequence: number }[],
  newestSequence: number,
  reservedTypes?: ReadonlySet<string>
): void {
  const ptyId = leaf.ptyId
  if (!ptyId) {
    return
  }
  const leafKey = deps.getLeafKey(leaf.tabId, leaf.leafId)
  const flight = deps.state.beginFlight(ptyId, mailboxHandle, reservedTypes)
  const writeResult = deps.writePty(ptyId, formatMessagePointer(unread.length, mailboxHandle))
  const finish = (accepted: boolean): void =>
    finishPointerWrite(
      deps,
      leafKey,
      leaf,
      mailboxHandle,
      unread,
      newestSequence,
      ptyId,
      flight,
      accepted
    )
  if (typeof writeResult === 'boolean') {
    finish(writeResult)
    return
  }
  void writeResult.then(finish, () => finish(false)).catch(() => undefined)
}

function finishPointerWrite<TWaiter extends OrchestrationMessageWaiter>(
  deps: PointerWriteDependencies<TWaiter>,
  leafKey: string,
  leaf: OrchestrationMailboxLeaf,
  mailboxHandle: string,
  unread: readonly { id: string; type: string; sequence: number }[],
  newestSequence: number,
  ptyId: string,
  flight: OrchestrationMailboxDeliveryFlight,
  accepted: boolean
): void {
  let delayedSettle = false
  try {
    if (!accepted || !deps.state.isCurrentFlight(ptyId, flight)) {
      return
    }
    const db = deps.getDb()
    if (
      !db ||
      shouldReleaseOrchestrationPointer(
        db,
        mailboxHandle,
        unread,
        deps.getMessageWaiters(mailboxHandle)
      )
    ) {
      return
    }
    flight.stagedMessageIds = unread.map((message) => message.id)
    db.markAsDelivered(flight.stagedMessageIds)
    // Why: carry this delivery's staged ids and reservedTypes onto the watermark record itself,
    // not just the flight - if settlement later deactivates (rather than clears) the watermark,
    // the flight is already gone by then and retirement needs the watermark to still know what
    // to roll back and what to redrive with (#14548 mailbox round).
    deps.state.setWatermark(mailboxHandle, newestSequence, ptyId, leafKey, {
      stagedMessageIds: flight.stagedMessageIds,
      reservedTypes: flight.reservedTypes
    })
    if (
      [leaf.lastOscTitle, leaf.paneTitle, deps.getTabTitle(leaf.tabId)].some(isCursorAgentTitle)
    ) {
      deps.state.clearWatermark(mailboxHandle, newestSequence, ptyId)
      deps.redrive(mailboxHandle)
      return
    }
    flight.enterTimer = setTimeout(
      () =>
        submitOrchestrationMailboxPointer(
          {
            mailboxOwner: deps.mailboxOwner,
            state: deps.state,
            getDb: deps.getDb,
            getLeaf: deps.getLeaf,
            getLeafKey: deps.getLeafKey,
            getMessageWaiters: deps.getMessageWaiters,
            isLeafPtyProvenAbsent: deps.isLeafPtyProvenAbsent,
            writePty: deps.writePty,
            settle: deps.settle,
            redrive: deps.redrive
          },
          { leaf, mailboxHandle, messages: unread, newestSequence, ptyId, flight }
        ),
      500
    )
    delayedSettle = true
  } finally {
    if (!delayedSettle) {
      deps.settle(ptyId, flight)
    }
  }
}
