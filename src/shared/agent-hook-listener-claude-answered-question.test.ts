import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearClaudeAnsweredQuestionWait,
  createHookListenerState,
  normalizeHookPayload,
  type HookListenerState
} from './agent-hook-listener'
import { clearGrokSessionPathLookupCacheForTests } from './grok-session-paths'
import { PANE_KEY } from './agent-hook-listener-test-harness'

describe('shared agent-hook-listener', () => {
  let state: HookListenerState

  beforeEach(() => {
    state = createHookListenerState()
  })

  afterEach(() => {
    clearGrokSessionPathLookupCacheForTests()
    vi.unstubAllEnvs()
  })

  describe('clearClaudeAnsweredQuestionWait', () => {
    const claudeEvent = (
      payload: Record<string, unknown>
    ): ReturnType<typeof normalizeHookPayload> =>
      normalizeHookPayload(state, 'claude', { paneKey: PANE_KEY, payload }, 'production')

    it('restores working for an answered lead question and drops the card', () => {
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'pick a color' })
      const wait = claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        tool_input: { questions: [{ question: 'Red or Blue?' }] }
      })
      expect(wait?.payload.state).toBe('waiting')
      expect(wait?.payload.interactivePrompt).toBeDefined()

      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({ state: 'working' })

      // Why: a child-driven refresh re-emits the cached lead state; the linger
      // bug would come back if it could resurrect the dismissed question.
      const childDriven = claudeEvent({
        hook_event_name: 'SubagentStart',
        agent_id: 'a1',
        agent_type: 'probe'
      })
      expect(childDriven?.payload.state).toBe('working')
      expect(childDriven?.payload.toolName).toBeUndefined()
      expect(childDriven?.payload.interactivePrompt).toBeUndefined()
    })

    it('restores the stashed lead state for an answered child question', () => {
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      claudeEvent({ hook_event_name: 'Stop' })
      const wait = claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        agent_id: 'a1',
        tool_input: { questions: [{ question: 'Continue?' }] }
      })
      expect(wait?.payload.state).toBe('waiting')

      // Why: the lead already finished; the answer resumes the child, so the
      // emitted state is gated up to working only while that child still runs.
      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({
        state: 'working',
        turnCompletedAt: expect.any(Number)
      })
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)).toEqual({
        state: 'done',
        turnCompletedAt: expect.any(Number)
      })

      const drained = claudeEvent({ hook_event_name: 'SubagentStop', agent_id: 'a1' })
      expect(drained?.payload.state).toBe('done')
    })

    it('falls back to working when no lead record exists', () => {
      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({ state: 'working' })
    })

    it('resolves an answered child question even after an unrelated lead event overwrote the cached lead record', () => {
      // Why: normalizeClaudeEvent unconditionally overwrites claudeLeadStateByPaneKey on every
      // event, including one wholly unrelated to any child — this reproduces exactly that: a
      // plain lead PreToolUse fires after the child's wait starts, and the answer must still
      // resolve the child's roster row instead of being silently dropped forever (#P1).
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      const wait = claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        agent_id: 'a1',
        tool_input: { questions: [{ question: 'Continue?' }] }
      })
      expect(wait?.payload.state).toBe('waiting')

      // Unrelated lead activity — no agent_id, so it doesn't touch child a1's own wait, but it
      // DOES overwrite the internal lead-turn cache away from 'waiting'/waitingAgentId.
      const unrelated = claudeEvent({ hook_event_name: 'PreToolUse', tool_name: 'Bash' })
      expect(unrelated?.payload.state).toBe('waiting')
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)?.state).not.toBe('waiting')

      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({ state: 'working' })
      expect(state.claudeSubagentRosterByPaneKey.get(PANE_KEY)?.get('a1')?.state).toBe('working')

      const drained = claudeEvent({ hook_event_name: 'SubagentStop', agent_id: 'a1' })
      expect(drained?.payload.state).toBe('working')
    })

    it('does not resolve a genuinely-blocked child when the LEAD itself asked the question', () => {
      // Why: a lead-owned AskUserQuestion (no agent_id) never sets waitingAgentId — answering it
      // must not accidentally resolve a DIFFERENT child's still-genuinely-pending
      // PermissionRequest just because a bare roster scan would otherwise find it first (#P1,
      // found reviewing an earlier version of this fix that used a bare scan with no fallback
      // restriction).
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      const blocked = claudeEvent({
        hook_event_name: 'PermissionRequest',
        agent_id: 'a1',
        tool_name: 'Bash',
        tool_input: { command: 'rm -rf build' }
      })
      expect(blocked?.payload.state).toBe('waiting')

      // The lead's OWN question — no agent_id, so it can't and doesn't touch a1's wait, but it
      // does overwrite the shared lead-turn cache's own state/waitingAgentId.
      const leadWait = claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        tool_input: { questions: [{ question: 'Proceed?' }] }
      })
      expect(leadWait?.payload.state).toBe('waiting')

      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({
        state: 'waiting'
      })
      // a1's real, still-pending Bash approval must survive untouched.
      expect(state.claudeSubagentRosterByPaneKey.get(PANE_KEY)?.get('a1')?.state).toBe('waiting')
      // Why: the STATE alone being correct isn't enough — a reader of lastToolByPaneKey must
      // still see a1's real card (not an empty one) immediately, not just once some later,
      // unrelated hook event happens to re-derive it (#P2, found reviewing an earlier version
      // of this fix that only repinned the tool snapshot inside the answeredChild branch, so a
      // lead-owned answer with a still-blocked child wiped the card instead of repinning it).
      expect(state.lastToolByPaneKey.get(PANE_KEY)).toMatchObject({
        toolName: 'Bash',
        toolInput: expect.stringContaining('rm -rf build')
      })
    })

    it('resolves the correct child when two are waiting at once', () => {
      // Why: answering the SECOND child's question must flip a2, not silently consume a1's
      // unrelated, still-pending permission request just because a1 was inserted first (#P1).
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a2', agent_type: 'probe' })
      const a1Blocked = claudeEvent({
        hook_event_name: 'PermissionRequest',
        agent_id: 'a1',
        tool_name: 'Bash',
        tool_input: { command: 'rm -rf build' }
      })
      expect(a1Blocked?.payload.state).toBe('waiting')
      const a2Wait = claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        agent_id: 'a2',
        tool_input: { questions: [{ question: 'Continue?' }] }
      })
      expect(a2Wait?.payload.state).toBe('waiting')

      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({ state: 'waiting' })
      const roster = state.claudeSubagentRosterByPaneKey.get(PANE_KEY)
      expect(roster?.get('a2')?.state).toBe('working')
      expect(roster?.get('a1')?.state).toBe('waiting')
      // Why: the repoint must record waitingOwner:'child' too, not just waitingAgentId — a
      // missing owner here forces a LATER answer for this same sibling through the less direct
      // fallback-scan path instead of the reliable pointer (found reviewing an earlier version
      // of this fix that set waitingAgentId but omitted waitingOwner on this exact write).
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)).toMatchObject({
        waitingAgentId: 'a1',
        waitingOwner: 'child'
      })
    })

    it('uses the pointer, not roster insertion order, when both children wait on their own AskUserQuestion', () => {
      // Why: this specifically pins the waitingOwner==='child' pointer branch — a1 (inserted
      // first) and a2 (inserted second, pointer names it) are BOTH AskUserQuestion-shaped, so
      // the isAskUserQuestionTool filter alone can't distinguish them; only the pointer can.
      // Deleting the pointer branch and falling straight to the fallback scan would wrongly
      // resolve a1 (first match) instead of a2 (the one the pointer, and the user, actually
      // answered) — this test fails in that case, unlike the earlier two-children test where
      // the non-question sibling gets filtered out regardless of whether the pointer runs.
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a2', agent_type: 'probe' })
      claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        agent_id: 'a1',
        tool_use_id: 'tu-a1',
        tool_input: { questions: [{ question: 'From a1?' }] }
      })
      claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        agent_id: 'a2',
        tool_use_id: 'tu-a2',
        tool_input: { questions: [{ question: 'From a2?' }] }
      })
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)).toMatchObject({
        waitingOwner: 'child',
        waitingAgentId: 'a2'
      })

      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({ state: 'waiting' })
      const roster = state.claudeSubagentRosterByPaneKey.get(PANE_KEY)
      expect(roster?.get('a2')?.state).toBe('working')
      expect(roster?.get('a1')?.state).toBe('waiting')
    })

    it('does not resolve a child genuinely waiting on its OWN AskUserQuestion when the lead asks a separate one', () => {
      // Why: waitingOwner:'lead' must short-circuit before ever considering a child row, even
      // when that child's own wait is itself an AskUserQuestion (the isAskUserQuestionTool
      // filter alone can't distinguish this from the lead's own question — both look identical
      // as "a waiting row with an AskUserQuestion tool name") (#P1).
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      const childWait = claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        agent_id: 'a1',
        tool_input: { questions: [{ question: 'Child question?' }] }
      })
      expect(childWait?.payload.state).toBe('waiting')

      // The lead's OWN question — no agent_id.
      const leadWait = claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        tool_input: { questions: [{ question: 'Lead question?' }] }
      })
      expect(leadWait?.payload.state).toBe('waiting')
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)?.waitingOwner).toBe('lead')

      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({ state: 'waiting' })
      // a1's real, still-pending question must survive untouched — the lead's own question was
      // what got answered, and there is no child row to flip for it.
      expect(state.claudeSubagentRosterByPaneKey.get(PANE_KEY)?.get('a1')?.state).toBe('waiting')
    })

    it('resolves whichever waiting question is currently displayed when the pointer is lost with two candidates', () => {
      // Why: once an unrelated event wipes waitingOwner/waitingAgentId, the fallback can no
      // longer identify "the one just answered" by insertion order alone when two children are
      // each waiting on their own AskUserQuestion — it must instead match whichever one's card
      // is actually still on screen (lastToolByPaneKey's waitingToolUseId), which the
      // unrelated-event handling itself keeps pinned to a real roster row throughout.
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a2', agent_type: 'probe' })
      claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        agent_id: 'a1',
        tool_use_id: 'tu-a1',
        tool_input: { questions: [{ question: 'From a1?' }] }
      })
      claudeEvent({
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        agent_id: 'a2',
        tool_use_id: 'tu-a2',
        tool_input: { questions: [{ question: 'From a2?' }] }
      })
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)?.waitingAgentId).toBe('a2')

      // Unrelated lead activity wipes waitingOwner/waitingAgentId — falls back to a roster scan.
      claudeEvent({ hook_event_name: 'PostToolUse', tool_name: 'Read' })
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)?.waitingOwner).toBeUndefined()

      // Why NOT rely on the real re-derivation here: claudeRosterFindWaitingSubagent (used by
      // both the unrelated-event handler AND, degenerately, the fallback's "first match" path)
      // always agrees with plain insertion order — so a naturally-occurring sequence can never
      // prove the discriminator does real work, only that it doesn't get in the way. Force a
      // genuine mismatch directly: pretend the currently-displayed card is a2's, even though a1
      // is first in the roster, and confirm the discriminator — not insertion order — wins.
      state.lastToolByPaneKey.set(PANE_KEY, {
        toolName: 'AskUserQuestion',
        waitingToolUseId: 'tu-a2'
      })

      expect(clearClaudeAnsweredQuestionWait(state, PANE_KEY)).toEqual({ state: 'waiting' })
      const roster = state.claudeSubagentRosterByPaneKey.get(PANE_KEY)
      // Resolves whichever is actually displayed (a2), not a1 merely because it's first.
      expect(roster?.get('a2')?.state).toBe('working')
      expect(roster?.get('a1')?.state).toBe('waiting')
    })

    it("carries a child's AskUserQuestion content through an unrelated lead event's re-derivation", () => {
      // Why: the roster had nowhere to store the actual question JSON before this fix — a
      // repointed/re-derived sibling's card showed the right toolName but permanently blank
      // content. This fails on pre-fix code (interactivePrompt undefined on both events).
      const expectedPrompt = JSON.stringify({ questions: [{ question: 'Pick a plan?' }] })
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      const wait = claudeEvent({
        hook_event_name: 'PreToolUse',
        agent_id: 'a1',
        tool_name: 'AskUserQuestion',
        tool_input: { questions: [{ question: 'Pick a plan?' }] }
      })
      // Why: the direct wait-inducing event already derives interactivePrompt live
      // (extractToolFields/deriveInteractivePrompt) — this half needs no fix, confirms the
      // baseline value to compare the re-derivation against below.
      expect(wait?.payload.interactivePrompt).toBe(expectedPrompt)

      // Why: a plain lead-level event with no agent_id forces effectiveState 'waiting' via the
      // still-waiting child (resolveClaudePaneState's "any waiting child wins" rule) and takes
      // the unrelatedWaitingChild re-derivation path, which repoints lastToolByPaneKey straight
      // from the roster row's FROZEN wait fields rather than deriving anything live.
      const unrelated = claudeEvent({ hook_event_name: 'PreToolUse', tool_name: 'Read' })
      expect(unrelated?.payload.state).toBe('waiting')
      expect(unrelated?.payload.toolName).toBe('AskUserQuestion')
      expect(unrelated?.payload.interactivePrompt).toBe(expectedPrompt)
    })

    it('carries the still-waiting sibling’s question content through clearClaudePendingWaitForAgent’s repoint', () => {
      // Why: covers the SubagentStop/TeammateIdle repoint site specifically — a1's real,
      // still-pending permission approval envelope must appear on the repointed snapshot, not
      // just its tool name.
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a2', agent_type: 'probe' })
      claudeEvent({
        hook_event_name: 'PermissionRequest',
        agent_id: 'a1',
        tool_name: 'Bash',
        tool_input: { command: 'rm -rf a' }
      })
      claudeEvent({
        hook_event_name: 'PreToolUse',
        agent_id: 'a2',
        tool_name: 'AskUserQuestion',
        tool_use_id: 'tu-a2',
        tool_input: { questions: [{ question: 'From a2?' }] }
      })
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)?.waitingAgentId).toBe('a2')

      // a2 stops without an explicit approval event — clearClaudePendingWaitForAgent repoints
      // the pane's single-slot wait pointer/tool-snapshot cache at a1, the still-waiting sibling.
      const stopped = claudeEvent({ hook_event_name: 'SubagentStop', agent_id: 'a2' })
      expect(stopped?.payload.state).toBe('waiting')
      expect(stopped?.payload.toolName).toBe('Bash')
      expect(stopped?.payload.interactivePrompt).toBe(
        JSON.stringify({ approval: { tool: 'Bash', summary: 'rm -rf a' } })
      )
    })

    it("carries the still-waiting sibling's question content through the child-answered-a-child-still-waiting repoint", () => {
      // Why: covers normalizeClaudeEvent's own repoint inside the subagentOriginId branch — a2's
      // own approval-granting tool event resolves a2 but must repoint the displayed card at a1,
      // the still-genuinely-waiting sibling, carrying a1's real question content along with it.
      const a1Prompt = JSON.stringify({ questions: [{ question: 'From a1?' }] })
      claudeEvent({ hook_event_name: 'UserPromptSubmit', prompt: 'go' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a1', agent_type: 'probe' })
      claudeEvent({ hook_event_name: 'SubagentStart', agent_id: 'a2', agent_type: 'probe' })
      claudeEvent({
        hook_event_name: 'PreToolUse',
        agent_id: 'a1',
        tool_name: 'AskUserQuestion',
        tool_use_id: 'tu-a1',
        tool_input: { questions: [{ question: 'From a1?' }] }
      })
      claudeEvent({
        hook_event_name: 'PreToolUse',
        agent_id: 'a2',
        tool_name: 'AskUserQuestion',
        tool_use_id: 'tu-a2',
        tool_input: { questions: [{ question: 'From a2?' }] }
      })
      expect(state.claudeLeadStateByPaneKey.get(PANE_KEY)?.waitingAgentId).toBe('a2')

      // a2's own next tool event (matching tool_use_id) grants its approval and resolves it.
      const granted = claudeEvent({
        hook_event_name: 'PostToolUse',
        agent_id: 'a2',
        tool_name: 'AskUserQuestion',
        tool_use_id: 'tu-a2',
        tool_response: { answer: 'yes' }
      })
      expect(granted?.payload.state).toBe('waiting')
      expect(granted?.payload.toolName).toBe('AskUserQuestion')
      expect(granted?.payload.interactivePrompt).toBe(a1Prompt)
      const roster = state.claudeSubagentRosterByPaneKey.get(PANE_KEY)
      expect(roster?.get('a2')?.state).toBe('working')
      expect(roster?.get('a1')?.state).toBe('waiting')
    })
  })
})
