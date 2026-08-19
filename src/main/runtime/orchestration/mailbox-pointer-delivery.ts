import { ORCHESTRATION_DELIVERY_BATCH_LIMIT, type OrchestrationDb } from './db'
import type { OrchestrationMailboxDeliveryTarget } from './mailbox-delivery-target'
import {
  isMailboxHandleDeliverableForOrigin,
  type OrchestrationMailboxDeliveryOrigin
} from './mailbox-pointer-delivery-origin'
import {
  hasUnfilteredOrchestrationWaiter,
  messageTypeHasOrchestrationWaiter,
  type OrchestrationMessageWaiter
} from './mailbox-pointer-eligibility'
import type { OrchestrationMailboxLeaf, OrchestrationMailboxOwner } from './mailbox-owner'
import {
  OrchestrationMailboxPointerState,
  type OrchestrationMailboxDeliveryFlight
} from './mailbox-pointer-state'
import { stageOrchestrationMailboxPointer } from './mailbox-pointer-write'

export type { OrchestrationMessageWaiter } from './mailbox-pointer-eligibility'
export type { OrchestrationMailboxDeliveryOrigin } from './mailbox-pointer-delivery-origin'

type PointerDeliveryDependencies<TWaiter extends OrchestrationMessageWaiter> = {
  mailboxOwner: OrchestrationMailboxOwner
  deliveryTarget: OrchestrationMailboxDeliveryTarget
  getDb: () => OrchestrationDb | null
  getLeaf: (leafKey: string) => OrchestrationMailboxLeaf | undefined
  getLeafKey: (tabId: string, leafId: string) => string
  getLiveLeafForHandle: (handle: string) => OrchestrationMailboxLeaf
  getMessageWaiters: (mailboxHandle: string) => ReadonlySet<TWaiter> | undefined
  getTabTitle: (tabId: string) => string | null | undefined
  getTerminalHandleForLeafKey: (leafKey: string) => string | undefined
  isLeafPtyProvenAbsent: (ptyId: string) => Promise<boolean>
  redriveMailbox: (
    mailboxHandle: string,
    reservedTypes?: ReadonlySet<string>,
    origin?: OrchestrationMailboxDeliveryOrigin
  ) => void
  writePty: (ptyId: string, data: string) => boolean | Promise<boolean>
}

export class OrchestrationMailboxPointerDelivery<TWaiter extends OrchestrationMessageWaiter> {
  private readonly state = new OrchestrationMailboxPointerState()
  constructor(private readonly deps: PointerDeliveryDependencies<TWaiter>) {}

  deliverForHandle(
    handle: string,
    reservedTypes?: ReadonlySet<string>,
    origin: OrchestrationMailboxDeliveryOrigin = 'notification'
  ): void {
    const terminalHandle = this.deps.deliveryTarget.resolveTerminalHandle(handle)
    if (!terminalHandle) {
      return
    }
    try {
      const leaf = this.deps.getLiveLeafForHandle(terminalHandle)
      if (leaf.lastAgentStatus !== 'idle' || !leaf.lastAgentStatusObservedLive) {
        return
      }
      const mailboxHandle = this.deps.mailboxOwner.resolve(leaf, handle)
      if (mailboxHandle) {
        this.deliver(leaf, { mailboxHandle, reservedTypes, origin })
      }
    } catch {
      // Persisted mail remains available to explicit check or a later idle edge.
    }
  }

  deliver(
    leaf: OrchestrationMailboxLeaf,
    options: {
      mailboxHandle: string
      origin: OrchestrationMailboxDeliveryOrigin
      reservedTypes?: ReadonlySet<string>
      skipAbsenceProbe?: boolean
    }
  ): void {
    const db = this.deps.getDb()
    const mailboxHandle = options.mailboxHandle
    if (!db || !isMailboxHandleDeliverableForOrigin(mailboxHandle, options.origin)) {
      return
    }
    if (!this.deps.getTerminalHandleForLeafKey(this.leafKey(leaf))) {
      return
    }
    if (
      mailboxHandle.startsWith('run:') &&
      db.hasOutstandingRunDelivery?.(mailboxHandle.slice('run:'.length))
    ) {
      return
    }
    if (leaf.ptyId && this.state.hasFlight(leaf.ptyId)) {
      this.state.parkDelivery(
        leaf.ptyId,
        mailboxHandle,
        leaf,
        options.origin,
        options.reservedTypes
      )
      return
    }
    if (this.state.hasActiveWatermark(mailboxHandle)) {
      this.parkRedelivery(mailboxHandle, options.reservedTypes)
      return
    }

    const waiters = this.deps.getMessageWaiters(mailboxHandle)
    if (hasUnfilteredOrchestrationWaiter(waiters)) {
      return
    }
    const excludedTypes = new Set(options.reservedTypes)
    for (const waiter of waiters ?? []) {
      for (const type of waiter.typeFilter ?? []) {
        excludedTypes.add(type)
      }
    }
    const unread = db
      .getUndeliveredUnreadMessages(mailboxHandle, undefined, {
        excludeTypes: [...excludedTypes],
        limit: ORCHESTRATION_DELIVERY_BATCH_LIMIT
      })
      .filter(
        (message) =>
          !options.reservedTypes?.has(message.type) &&
          !messageTypeHasOrchestrationWaiter(waiters, message.type)
      )
      .slice(0, ORCHESTRATION_DELIVERY_BATCH_LIMIT)
    if (unread.length === 0 || !leaf.writable || !leaf.ptyId) {
      return
    }
    const newestSequence = unread.at(-1)?.sequence
    if (newestSequence === undefined) {
      return
    }
    if (
      !this.state.releaseSupersededWatermark(
        mailboxHandle,
        newestSequence,
        leaf.ptyId,
        this.leafKey(leaf)
      )
    ) {
      return
    }
    if (
      this.deps.deliveryTarget.deferForAbsenceProbe(
        leaf,
        mailboxHandle,
        options.skipAbsenceProbe,
        (probedLeaf, ptyId, probedMailbox) =>
          this.redeliverAfterProbe(probedLeaf, ptyId, probedMailbox, options.origin)
      )
    ) {
      return
    }
    stageOrchestrationMailboxPointer(
      this.writeDeps(),
      leaf,
      mailboxHandle,
      unread,
      newestSequence,
      options.reservedTypes
    )
  }

  parkRedelivery(mailboxHandle: string, reservedTypes?: ReadonlySet<string>): void {
    this.state.parkRedelivery(mailboxHandle, reservedTypes)
  }

  retirePty(ptyId: string): void {
    const { flight, stagedMessageIds, redrives } = this.state.retirePty(ptyId)
    if (flight?.enterTimer != null) {
      clearTimeout(flight.enterTimer)
    }
    if (stagedMessageIds.length) {
      // Why: staged ids can come from the in-flight flight AND/OR a deactivated watermark
      // (submission settled without ever sending Enter) - both leave messages marked
      // 'delivered' with no live PTY left to submit them, so both must roll back here or
      // they are lost for good once this ptyId is gone (#14548 mailbox round).
      this.deps.getDb()?.markAsUndelivered(stagedMessageIds)
    }
    // Why: a mailbox's reservation can be spread across the flight, a watermark it owns, and a
    // separately parked delivery - state.retirePty() already coalesced those into one redrive
    // per mailbox with merged reservedTypes, so redrive each exactly once here instead of the
    // three independent loops this used to be (which could double-redrive or drop a source).
    for (const [mailboxHandle, reservedTypes] of redrives) {
      this.parkRedelivery(mailboxHandle, reservedTypes)
      this.redrive(mailboxHandle, true)
    }
  }

  private redeliverAfterProbe(
    leaf: OrchestrationMailboxLeaf,
    ptyId: string,
    mailboxHandle: string,
    origin: OrchestrationMailboxDeliveryOrigin
  ): void {
    const currentLeaf = this.deps.getLeaf(this.leafKey(leaf))
    if (
      currentLeaf?.ptyId === ptyId &&
      currentLeaf.lastAgentStatus === 'idle' &&
      currentLeaf.lastAgentStatusObservedLive
    ) {
      this.deliver(currentLeaf, { mailboxHandle, origin, skipAbsenceProbe: true })
    }
  }

  // Why: bridges this class's `deps`/`state`/callbacks into the plain deps bag the
  // extracted pointer-write helper expects (kept as a free function for max-lines budget).
  private writeDeps() {
    return {
      mailboxOwner: this.deps.mailboxOwner,
      state: this.state,
      getDb: this.deps.getDb,
      getLeaf: this.deps.getLeaf,
      getLeafKey: this.deps.getLeafKey,
      getMessageWaiters: this.deps.getMessageWaiters,
      getTabTitle: this.deps.getTabTitle,
      isLeafPtyProvenAbsent: this.deps.isLeafPtyProvenAbsent,
      writePty: this.deps.writePty,
      settle: (ptyId: string, flight: OrchestrationMailboxDeliveryFlight) =>
        this.settle(ptyId, flight),
      redrive: (mailboxHandle: string, force?: boolean) => this.redrive(mailboxHandle, force)
    }
  }

  private settle(ptyId: string, flight: OrchestrationMailboxDeliveryFlight): void {
    const parked = this.state.settleFlight(ptyId, flight)
    if (!parked) {
      return
    }
    for (const [mailboxHandle, delivery] of parked) {
      const currentLeaf = this.deps.getLeaf(this.leafKey(delivery.leaf))
      if (
        currentLeaf?.ptyId !== ptyId ||
        this.deps.mailboxOwner.resolve(currentLeaf, mailboxHandle) !== mailboxHandle
      ) {
        this.parkRedelivery(mailboxHandle, delivery.reservedTypes)
        this.redrive(mailboxHandle)
      } else {
        this.deliver(currentLeaf, {
          mailboxHandle,
          origin: delivery.origin,
          reservedTypes: delivery.reservedTypes
        })
      }
    }
  }

  private redrive(mailboxHandle: string, force = false): void {
    const parkedTypes = this.state.takeRedelivery(mailboxHandle, force)
    if (parkedTypes === undefined) {
      return
    }
    queueMicrotask(() => {
      try {
        // Anything reaching redrive already passed the origin gate once, so a dispatch:
        // mailbox interrupted mid-delivery isn't stranded behind the notification-only gate.
        this.deps.redriveMailbox(mailboxHandle, parkedTypes ?? undefined, 'idle-transition')
      } catch {
        // Durable mail remains available to explicit check or a later idle edge.
      }
    })
  }

  private leafKey(leaf: OrchestrationMailboxLeaf): string {
    return this.deps.getLeafKey(leaf.tabId, leaf.leafId)
  }
}
