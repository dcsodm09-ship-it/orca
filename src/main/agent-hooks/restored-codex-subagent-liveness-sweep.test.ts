import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentSubagentSnapshot } from '../../shared/agent-status-types'
import { upsertCodexSubagent } from '../../shared/codex-subagent-roster'
import { makePaneKey } from '../../shared/stable-pane-id'
import { AgentHookServer } from './server'
import { postHookEvent } from './server.test-fixtures'
import {
  isLocalExecutionHost,
  sweepRestoredSubagentsWithoutLiveAgent
} from './restored-subagent-liveness-sweep'

const LEAF = '11111111-1111-4111-8111-111111111111'
const PANE = makePaneKey('tab-1', LEAF)
const PTY = 'wt-1__pty-1'

let dir: string

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'orca-restored-subagent-'))
})

afterEach(() => {
  vi.restoreAllMocks()
  rmSync(dir, { recursive: true, force: true })
})

function paneStatus(
  server: AgentHookServer,
  paneKey = PANE
): { state: string; subagents?: AgentSubagentSnapshot[] } {
  const entry = server.getStatusSnapshotForPane(paneKey)[0]
  return { state: entry?.state ?? 'missing', subagents: entry?.subagents }
}

const CODEX_WORKING_CHILD: AgentSubagentSnapshot = {
  id: 'child-thread-1',
  state: 'working',
  startedAt: 1_000
}
const CODEX_WAITING_CHILD: AgentSubagentSnapshot = {
  id: 'child-thread-2',
  state: 'waiting',
  startedAt: 1_000
}

/** Codex counterpart of restartWithInFlightSubagent (in restored-subagent-liveness-sweep.test.ts)
 *  — same hydrate-then-restart shape (lead finished/waiting, roster still holds a working child
 *  whose SubagentStop was lost while Orca was down), but for a Codex pane. */
async function restartWithInFlightCodexSubagent(options?: {
  connectionId?: string
  state?: 'working' | 'waiting'
  subagents?: AgentSubagentSnapshot[]
}): Promise<AgentHookServer> {
  const first = new AgentHookServer()
  await first.start({ env: 'production', userDataPath: dir })
  first.ingestTerminalStatus({
    paneKey: PANE,
    tabId: 'tab-1',
    worktreeId: 'wt-1',
    connectionId: options?.connectionId ?? null,
    payload: {
      state: options?.state ?? 'working',
      prompt: 'review the PR',
      agentType: 'codex',
      subagents: options?.subagents ?? [CODEX_WORKING_CHILD]
    }
  })
  first.flushStatusPersistSync()
  first.stop()

  const restarted = new AgentHookServer()
  await restarted.start({ env: 'production', userDataPath: dir })
  return restarted
}

function sweepCodexWith(
  server: AgentHookServer,
  overrides: {
    probeLiveLocalPty?: (ptyId: string) => boolean | null | Promise<boolean | null>
    executionHostId?: string | null
    boundPtyIdByPaneKey?: Record<string, string>
    persistedPtyIdByPaneKey?: Record<string, string>
  } = {}
): Promise<number> {
  return sweepRestoredSubagentsWithoutLiveAgent({
    probeLiveLocalPty: async (ptyId) =>
      overrides.probeLiveLocalPty ? await overrides.probeLiveLocalPty(ptyId) : false,
    isLocalExecutionHost: () =>
      isLocalExecutionHost(
        overrides.executionHostId === undefined ? 'local' : overrides.executionHostId
      ),
    getBoundPtyIdForPaneKey: (paneKey) => overrides.boundPtyIdByPaneKey?.[paneKey],
    getPersistedPtyIdForPaneKey: (paneKey) => overrides.persistedPtyIdByPaneKey?.[paneKey],
    reap: (isLocalHost, isLocalPaneAgentLive, isLocalPaneLivenessEvidenceCurrent) =>
      server.reapRestoredCodexSubagentsWithoutLiveAgent(
        isLocalHost,
        isLocalPaneAgentLive,
        isLocalPaneLivenessEvidenceCurrent
      )
  })
}

describe('restored codex subagent liveness sweep', () => {
  it('reaps the phantom seed so a slept-through codex pane reaches done', async () => {
    const server = await restartWithInFlightCodexSubagent()
    try {
      expect(paneStatus(server)).toEqual({ state: 'working', subagents: [CODEX_WORKING_CHILD] })
      const previous = server.getStatusSnapshotForPane(PANE)[0]
      expect(previous?.restoredUnconfirmed).toBe(true)
      const previousReceivedAt = previous?.receivedAt ?? 0
      const reconciledAt = previousReceivedAt + 1
      const now = vi.spyOn(Date, 'now').mockReturnValue(previousReceivedAt)

      expect(await sweepCodexWith(server, { persistedPtyIdByPaneKey: { [PANE]: PTY } })).toBe(1)

      expect(paneStatus(server)).toEqual({ state: 'done', subagents: undefined })
      expect(server.getStatusSnapshotForPane(PANE)[0]).toMatchObject({
        receivedAt: reconciledAt,
        stateStartedAt: reconciledAt
      })
      // Why: the reconciled `done` is process-probe-verified, so it must shed the hydrated-unconfirmed marker.
      expect(server.getStatusSnapshotForPane(PANE)[0]?.restoredUnconfirmed).toBeUndefined()
      now.mockRestore()
    } finally {
      server.stop()
    }
  })

  it('keeps a codex seed whose pane still has a live local PTY', async () => {
    const server = await restartWithInFlightCodexSubagent()
    try {
      expect(
        await sweepCodexWith(server, {
          probeLiveLocalPty: (ptyId) => ptyId === PTY,
          persistedPtyIdByPaneKey: { [PANE]: PTY }
        })
      ).toBe(0)

      expect(paneStatus(server)).toEqual({ state: 'working', subagents: [CODEX_WORKING_CHILD] })
    } finally {
      server.stop()
    }
  })

  it('clears restoredFromSnapshot once a live hook confirms the same child id, so the sweep leaves it alone', async () => {
    const server = await restartWithInFlightCodexSubagent()
    try {
      const roster = server._getStateForTests().codexSubagentRosterByPaneKey.get(PANE)
      expect(roster?.get(CODEX_WORKING_CHILD.id)?.restoredFromSnapshot).toBe(true)

      // Why: a live SubagentStart/PreToolUse hook on this exact id is the only
      // thing that legitimately proves the hydrated row is still the current
      // process, not a phantom left by one that died while Orca was down.
      upsertCodexSubagent(
        roster!,
        CODEX_WORKING_CHILD.id,
        { state: 'working', source: 'hook' },
        Date.now()
      )
      expect(roster?.get(CODEX_WORKING_CHILD.id)?.restoredFromSnapshot).toBeUndefined()

      expect(await sweepCodexWith(server, { persistedPtyIdByPaneKey: { [PANE]: PTY } })).toBe(0)

      expect(paneStatus(server).state).toBe('working')
    } finally {
      server.stop()
    }
  })

  it('reaps a persisted waiting child so a slept-through waiting codex pane reaches done', async () => {
    const server = await restartWithInFlightCodexSubagent({
      state: 'waiting',
      subagents: [CODEX_WAITING_CHILD]
    })
    try {
      expect(paneStatus(server)).toEqual({ state: 'waiting', subagents: [CODEX_WAITING_CHILD] })
      const previous = server.getStatusSnapshotForPane(PANE)[0]
      expect(previous?.restoredUnconfirmed).toBe(true)
      const previousReceivedAt = previous?.receivedAt ?? 0
      const reconciledAt = previousReceivedAt + 1
      const now = vi.spyOn(Date, 'now').mockReturnValue(previousReceivedAt)

      expect(await sweepCodexWith(server, { persistedPtyIdByPaneKey: { [PANE]: PTY } })).toBe(1)

      expect(paneStatus(server)).toEqual({ state: 'done', subagents: undefined })
      expect(server.getStatusSnapshotForPane(PANE)[0]).toMatchObject({
        receivedAt: reconciledAt,
        stateStartedAt: reconciledAt
      })
      expect(server.getStatusSnapshotForPane(PANE)[0]?.restoredUnconfirmed).toBeUndefined()
      now.mockRestore()
    } finally {
      server.stop()
    }
  })

  it('never marks a genuinely-alive codex pane restoredUnconfirmed just because an unrelated ghost child sits unreaped', async () => {
    // Why: locks in the dual-review finding (2026-08-20) — routing the reap sweep's own need
    // through the pane-wide restoredUnconfirmed field pinned this exact scenario (live lead, many
    // real turns, one restored child the startup liveness probe correctly spared) "unconfirmed"
    // for up to 30 min: blank mobile agent rows, unanswerable remote AskUserQuestion, suppressed
    // attention, for a pane with nothing wrong. Codex never computes restoredUnconfirmed now; the
    // sweep answers its narrower question directly against the roster (see
    // reapRestoredCodexSubagentsWithoutLiveAgent). Drives 5 real turns, asserts it stays unset.
    const server = await restartWithInFlightCodexSubagent({
      state: 'working',
      subagents: [CODEX_WORKING_CHILD]
    })
    try {
      const lead = (hookEventName: string, extra: Record<string, unknown> = {}) =>
        postHookEvent(
          server,
          {
            paneKey: PANE,
            tabId: 'tab-1',
            worktreeId: 'wt-1',
            env: 'production',
            payload: { hook_event_name: hookEventName, ...extra }
          },
          '/hook/codex'
        )
      for (let turn = 1; turn <= 5; turn += 1) {
        await lead('UserPromptSubmit', { prompt: `live turn ${turn}` })
        await lead('PreToolUse', { tool_name: 'shell' })
        await lead('PostToolUse', { tool_name: 'shell' })
        await lead('Stop')
        expect(server.getStatusSnapshotForPane(PANE)[0]?.restoredUnconfirmed).toBeUndefined()
      }
    } finally {
      server.stop()
    }
  })

  it('reaps a persisted waiting child so a slept-through waiting codex pane reaches done, surviving a delayed Stop first', async () => {
    // Why: closes the Codex-side mirror of gap-1's dual-review finding (2026-08-20) — the reap
    // sweep's candidate filter used to require !runtimeObservedStatusPaneKeys.has(paneKey), a
    // pane-wide flag ANY hook event sets (including the dead process's own delayed Stop),
    // permanently excluding the pane from reapRestoredCodexSubagentsWithoutLiveAgent. Two Set/
    // marker-based fixes were tried and both reverted after a further dual-review round found
    // they made an otherwise-genuinely-alive Codex pane with one unrelated unreaped ghost read
    // "unconfirmed" to ~6 unrelated consumers (mobile agent rows vanished, remote AskUserQuestion
    // unanswerable, attention suppressed) for up to 30 minutes. The final fix instead makes the
    // reap sweep's candidate filter check codexRosterHasRuntimeConfirmedSubagent directly against
    // the roster — no pane-wide flag at all, so this delayed Stop can no longer defeat the sweep,
    // and nothing leaks to unrelated consumers. The Claude counterpart of this test (above,
    // 'surviving an unrelated event first') covers the analogous Claude-side mechanism, which
    // legitimately IS pane-wide (restoredUnconfirmed) — the two sides are no longer symmetric by
    // design, see the status report for why.
    const server = await restartWithInFlightCodexSubagent({
      state: 'waiting',
      subagents: [CODEX_WAITING_CHILD]
    })
    try {
      expect(paneStatus(server)).toEqual({ state: 'waiting', subagents: [CODEX_WAITING_CHILD] })

      await postHookEvent(
        server,
        {
          paneKey: PANE,
          tabId: 'tab-1',
          worktreeId: 'wt-1',
          env: 'production',
          payload: { hook_event_name: 'Stop' }
        },
        '/hook/codex'
      )
      const afterDelayedStop = server.getStatusSnapshotForPane(PANE)[0]
      expect(afterDelayedStop).toMatchObject({ state: 'waiting' })

      const previousReceivedAt = afterDelayedStop?.receivedAt ?? 0
      const reconciledAt = previousReceivedAt + 1
      const now = vi.spyOn(Date, 'now').mockReturnValue(previousReceivedAt)

      expect(await sweepCodexWith(server, { persistedPtyIdByPaneKey: { [PANE]: PTY } })).toBe(1)

      expect(paneStatus(server)).toEqual({ state: 'done', subagents: undefined })
      expect(server.getStatusSnapshotForPane(PANE)[0]).toMatchObject({
        receivedAt: reconciledAt,
        stateStartedAt: reconciledAt
      })
      now.mockRestore()
    } finally {
      server.stop()
    }
  })

  it('reaps a persisted waiting child so a slept-through waiting codex pane reaches done, surviving a delayed NON-Stop lead event first', async () => {
    // Why: a Stop-only fix (tried first, dual review 2026-08-20) reproducibly missed this exact
    // case — a delayed lead-level PostToolUse (no agent_id) arriving before the sweep still let
    // the pane-wide runtimeObservedStatusPaneKeys flag defeat the sweep, since only the Stop
    // branch ever cleared it. The final fix (see the previous test's comment) removes the
    // pane-wide flag from the Codex reap path entirely, so no event type can defeat it.
    const server = await restartWithInFlightCodexSubagent({
      state: 'waiting',
      subagents: [CODEX_WAITING_CHILD]
    })
    try {
      expect(paneStatus(server)).toEqual({ state: 'waiting', subagents: [CODEX_WAITING_CHILD] })

      await postHookEvent(
        server,
        {
          paneKey: PANE,
          tabId: 'tab-1',
          worktreeId: 'wt-1',
          env: 'production',
          payload: { hook_event_name: 'PostToolUse', tool_name: 'shell' }
        },
        '/hook/codex'
      )
      const afterDelayedEvent = server.getStatusSnapshotForPane(PANE)[0]
      expect(afterDelayedEvent).toMatchObject({ state: 'waiting' })

      const previousReceivedAt = afterDelayedEvent?.receivedAt ?? 0
      const reconciledAt = previousReceivedAt + 1
      const now = vi.spyOn(Date, 'now').mockReturnValue(previousReceivedAt)

      expect(await sweepCodexWith(server, { persistedPtyIdByPaneKey: { [PANE]: PTY } })).toBe(1)

      expect(paneStatus(server)).toEqual({ state: 'done', subagents: undefined })
      expect(server.getStatusSnapshotForPane(PANE)[0]).toMatchObject({
        receivedAt: reconciledAt,
        stateStartedAt: reconciledAt
      })
      now.mockRestore()
    } finally {
      server.stop()
    }
  })

  it("does not resolve a lead-owned codex wait just because an unrelated dead child's row gets reaped", async () => {
    // Why: mirrors the Claude-side 'preserves timing when reaping rows does not change the
    // lead state' guard — proves hadWaitingSubagentBeforeReap actually gates the Codex fix,
    // not just cosmetically mirrors the Claude one.
    const server = await restartWithInFlightCodexSubagent({
      state: 'waiting',
      subagents: [CODEX_WORKING_CHILD]
    })
    try {
      expect(paneStatus(server)).toEqual({ state: 'waiting', subagents: [CODEX_WORKING_CHILD] })

      expect(await sweepCodexWith(server, { persistedPtyIdByPaneKey: { [PANE]: PTY } })).toBe(1)

      expect(paneStatus(server)).toEqual({ state: 'waiting', subagents: undefined })
    } finally {
      server.stop()
    }
  })

  it('never reaps a relay-owned codex pane even with no local PTY at all', async () => {
    const server = await restartWithInFlightCodexSubagent({ connectionId: 'conn-1' })
    try {
      expect(await sweepCodexWith(server)).toBe(0)

      expect(paneStatus(server).state).toBe('working')
    } finally {
      server.stop()
    }
  })
})
