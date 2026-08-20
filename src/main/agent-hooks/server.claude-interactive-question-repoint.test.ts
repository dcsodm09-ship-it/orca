import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentQuestionAnsweredInferenceRequest } from '../../shared/agent-question-answered-intent'
import { AgentHookServer } from './server'
import { buildBody, PANE, postHookEvent } from './server.test-fixtures'

const { getCohortAtEmitMock, trackMock } = vi.hoisted(() => ({
  getCohortAtEmitMock: vi.fn(),
  trackMock: vi.fn()
}))

vi.mock('../telemetry/client', () => ({ track: trackMock }))
vi.mock('../telemetry/cohort-classifier', () => ({ getCohortAtEmit: getCohortAtEmitMock }))

beforeEach(() => {
  trackMock.mockReset()
  getCohortAtEmitMock.mockReset()
  getCohortAtEmitMock.mockReturnValue({ nth_repo_added: 2 })
})

afterEach(() => {
  vi.restoreAllMocks()
})

// Why: mirrors answeredRequestFromSnapshot in server.claude-interactive-question.test.ts — kept
// as a local copy rather than an import since that helper isn't exported, and duplicating an
// 8-line snapshot-to-baseline mapper is cheaper than widening that file's public surface.
function answeredRequestFromSnapshot(
  server: AgentHookServer
): AgentQuestionAnsweredInferenceRequest {
  const [entry] = server.getStatusSnapshot()
  return {
    paneKey: entry.paneKey,
    baselineUpdatedAt: entry.receivedAt,
    baselineStateStartedAt: entry.stateStartedAt,
    baselinePrompt: entry.prompt as string,
    baselineAgentType: entry.agentType
  }
}

describe('inferQuestionAnswered repoints onto a still-waiting child (GAP 2)', () => {
  it("publishes the still-waiting child's real toolName/interactivePrompt/subagents immediately, not a bare {state:'waiting'}", async () => {
    const server = new AgentHookServer()
    await server.start({ env: 'production' })
    try {
      const childPrompt = JSON.stringify({
        questions: [{ question: 'Pick a plan?', options: ['A', 'B'] }]
      })
      await postHookEvent(server, buildBody({ hook_event_name: 'UserPromptSubmit', prompt: 'go' }))
      // Child A's own AskUserQuestion wait.
      await postHookEvent(
        server,
        buildBody({
          hook_event_name: 'PreToolUse',
          agent_id: 'a-child-1',
          agent_type: 'general-purpose',
          tool_name: 'AskUserQuestion',
          tool_input: { questions: [{ question: 'Pick a plan?', options: ['A', 'B'] }] }
        })
      )
      expect(server.getStatusSnapshot()[0]).toMatchObject({
        state: 'waiting',
        toolName: 'AskUserQuestion',
        subagents: [expect.objectContaining({ id: 'a-child-1', state: 'waiting' })]
      })

      // The LEAD's OWN AskUserQuestion (no agent_id) displaces child A's card and becomes
      // `existing.payload` — waitingOwner:'lead'.
      await postHookEvent(
        server,
        buildBody({
          hook_event_name: 'PreToolUse',
          tool_name: 'AskUserQuestion',
          tool_input: { questions: [{ question: "Lead's own question?" }] }
        })
      )
      expect(server.getStatusSnapshot()[0]).toMatchObject({
        state: 'waiting',
        toolName: 'AskUserQuestion',
        interactivePrompt: JSON.stringify({ questions: [{ question: "Lead's own question?" }] })
      })

      // Simulate the lead's own question being answered (submit keystroke inferred, no further
      // hook posted) — this is the exact call inferQuestionAnswered's own gate expects.
      const baseline = answeredRequestFromSnapshot(server)
      expect(server.inferQuestionAnswered(baseline)).toBe(true)

      // Why: assert IMMEDIATELY, with no intervening hook event — this fails two independent
      // ways on pre-fix code: (1) server.ts built the published payload only from
      // restored.state + the stale pre-answer payload.subagents, never consulting
      // lastToolByPaneKey, so this read a bare {state:'waiting'} with no toolName at all; (2)
      // even with (1) fixed, TrackedClaudeSubagent had nowhere to store the actual question
      // JSON, so interactivePrompt would still be undefined.
      const status = server.getStatusSnapshot()[0]
      expect(status).toMatchObject({
        paneKey: PANE,
        state: 'waiting',
        toolName: 'AskUserQuestion',
        interactivePrompt: childPrompt,
        subagents: [expect.objectContaining({ id: 'a-child-1', state: 'waiting' })]
      })
    } finally {
      server.stop()
    }
  })
})

describe('inferQuestionAnswered on a relayed pane preserves subagents (P2)', () => {
  it('falls back to the pre-answer payload.subagents when the local roster was never populated', async () => {
    const server = new AgentHookServer()
    await server.start({ env: 'production' })
    try {
      // Why: a relayed/remote Claude pane never runs normalizeClaudeEvent locally — only
      // Codex has a reconcileRemoteCodexState equivalent — so
      // claudeSubagentRosterByPaneKey is never populated for this paneKey. This ingests a
      // status shaped exactly like what the relay channel would deliver, with subagents
      // carried directly on the wire payload rather than derived from a local roster.
      server.ingestRemote(
        {
          paneKey: PANE,
          payload: {
            state: 'waiting',
            prompt: 'go',
            agentType: 'claude',
            toolName: 'AskUserQuestion',
            toolInput: JSON.stringify({ questions: [{ question: 'Pick a plan?' }] }),
            interactivePrompt: JSON.stringify({ questions: [{ question: 'Pick a plan?' }] }),
            subagents: [{ id: 'a-child-1', state: 'waiting', startedAt: 0 }]
          }
        },
        'relay-conn-1'
      )
      expect(server.getStatusSnapshot()[0]).toMatchObject({
        subagents: [expect.objectContaining({ id: 'a-child-1', state: 'waiting' })]
      })

      const baseline = answeredRequestFromSnapshot(server)
      expect(server.inferQuestionAnswered(baseline)).toBe(true)

      // Why: pre-fix, currentSubagents came only from claudeRosterToSnapshots(local roster),
      // which is undefined for a relay-only pane — the `currentSubagents ? {...} : {}` spread
      // then omitted the subagents key entirely, and applyNormalizedStatus replaces the cached
      // payload wholesale rather than merging, so answering blanked child-row cards that were
      // present the instant before on a real SSH/WSL/remote pane.
      expect(server.getStatusSnapshot()[0]).toMatchObject({
        paneKey: PANE,
        subagents: [expect.objectContaining({ id: 'a-child-1', state: 'waiting' })]
      })
    } finally {
      server.stop()
    }
  })
})
