import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createHookListenerState,
  normalizeHookPayload,
  type HookListenerState
} from './agent-hook-listener'
import { clearGrokSessionPathLookupCacheForTests } from './grok-session-paths'
import {
  CLAUDE_PREVIOUS_PROMPT_ID,
  CLAUDE_PROMPT_ID,
  normalizeAndAccept,
  PANE_KEY
} from './agent-hook-listener-test-harness'

describe('shared agent-hook-listener', () => {
  let state: HookListenerState

  beforeEach(() => {
    state = createHookListenerState()
  })

  afterEach(() => {
    clearGrokSessionPathLookupCacheForTests()
    vi.unstubAllEnvs()
  })

  it('resolves a Claude Stop to done even while the session sits in plan mode (known gap, follow-up pending)', () => {
    // Why: `permission_mode` is the CLI's current session-wide permission setting, not a
    // per-event "a plan is pending approval" flag — it stays 'plan' for the whole session
    // when a user has plan mode as their default, including on ordinary turns that finished
    // normally. Gating this Stop to 'waiting' (attempted for #10997) mapped every such turn to
    // the runtime's 'permission' status, which made writeTerminalAgentPrompt's
    // assertAgentPromptPermissionSafe throw agent_prompt_blocked on a genuinely idle worker and
    // broke tui-idle/push-on-idle detection. No reliable field has been found in a real Stop
    // payload to distinguish "paused for plan confirmation" from "finished while in plan mode",
    // so this documents today's behavior rather than mis-fixing it.
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'plan it' })

    const event = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'Stop',
      permission_mode: 'plan'
    })

    expect(event?.payload.state).toBe('done')
    expect(event?.payload.interrupted).toBeUndefined()
  })

  it('lets an interrupt still win over plan mode on Claude Stop', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'plan it' })

    const event = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'Stop',
      permission_mode: 'plan',
      is_interrupt: true
    })

    expect(event?.payload.state).toBe('done')
    expect(event?.payload.interrupted).toBe(true)
  })

  it('keeps Claude StopFailure resolving to done regardless of plan mode', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'plan it' })

    const event = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'StopFailure',
      permission_mode: 'plan'
    })

    // Why: StopFailure has no plan-approval semantics, so plan mode must not gate it to waiting.
    expect(event?.payload.state).toBe('done')
  })

  it('gates a bare Stop right after an unresolved PermissionRequest to waiting instead of done (#10997)', () => {
    // Why: a real PermissionDenied hook (distinct from PermissionRequest) is the one reliable
    // signal a Stop payload itself never carries; pendingPermissionRequest stands in for it so a
    // Stop with no intervening decision can't be silently read as a legitimate finish.
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    const waiting = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /' }
    })
    expect(waiting?.payload.state).toBe('waiting')

    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('waiting')
  })

  it('resolves to done once the tool runs after approval (approval-granted path, #10997)', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    // Why: approval lets the tool actually run, clearing pendingPermissionRequest same as any
    // other non-PermissionRequest event.
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })

    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('done')
  })

  it('resolves to done once Claude reports the denial (denial path, #10997)', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /' }
    })
    const denied = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionDenied',
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /' },
      reason: 'user_denied'
    })
    expect(denied?.payload.state).toBe('working')

    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('done')
  })

  it('does not gate a second consecutive Stop (#11352-class regression guard)', () => {
    // Why: pendingPermissionRequest is single-shot — the gated Stop above must not wedge the
    // pane in 'waiting' forever the way the old permission_mode heuristic (#11352) did.
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /' }
    })
    const firstStop = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(firstStop?.payload.state).toBe('waiting')

    const secondStop = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(secondStop?.payload.state).toBe('done')
  })

  it('keeps a lead Stop gated to waiting while a sibling child still has a pending PermissionRequest (#10997 child-agent gap)', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-b',
      tool_name: 'Bash',
      tool_input: { command: 'pwd' }
    })

    // Child A's own denial clears only child A's pending entry. The pane's reported state
    // still reflects the lead's own cached status (still 'waiting', mid-turn) - that's the
    // pre-existing cached-lead-status behavior for any child event, not what this fix changes.
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionDenied',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' },
      reason: 'user_denied'
    })

    // Child B never resolved its own request, so the lead Stop must still gate to waiting.
    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('waiting')
  })

  it('does not let an unrelated sibling event clear a different agent pending PermissionRequest', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /' }
    })
    // Child B runs an unrelated, already-approved tool - must not touch child A's entry.
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PostToolUse',
      agent_id: 'child-b',
      tool_name: 'Read',
      tool_input: { file_path: '/etc/hosts' }
    })

    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('waiting')
  })

  it('resolves the lead Stop to done once every agent with a pending PermissionRequest has cleared its own', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-b',
      tool_name: 'Bash',
      tool_input: { command: 'pwd' }
    })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionDenied',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' },
      reason: 'user_denied'
    })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionDenied',
      agent_id: 'child-b',
      tool_name: 'Bash',
      tool_input: { command: 'pwd' },
      reason: 'user_denied'
    })
    // Why: both children finish (not just resolve their permission) so resolveClaudePaneState's
    // separate, pre-existing "roster still has a working child" override doesn't independently
    // keep the pane at 'working' - that's unrelated to this fix, isolating what it actually tests.
    normalizeAndAccept(state, 'claude', { hook_event_name: 'SubagentStop', agent_id: 'child-a' })
    normalizeAndAccept(state, 'claude', { hook_event_name: 'SubagentStop', agent_id: 'child-b' })

    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('done')
  })

  it('restores the pane from waiting once its sole pending child denies, without needing a later lead Stop (#10997 child-agent gap)', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /' }
    })

    // Why: with no sibling still pending, the child's own denial must itself restore the
    // pane out of 'waiting' - otherwise it stays cache-rendered 'waiting' forever, since
    // nothing else re-evaluates state until the next real lead-level Stop/StopFailure.
    const denied = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionDenied',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /' },
      reason: 'user_denied'
    })
    expect(denied?.payload.state).toBe('working')
  })

  it('keeps a sibling pending PermissionRequest alive when a different child gets its own approval-granted tool run (#10997 child-agent gap)', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    // child-b requests first, then child-a - child-a becomes the tracked waitingAgentId.
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-b',
      tool_name: 'Bash',
      tool_input: { command: 'pwd' }
    })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })

    // child-a's own tool runs (approval granted) - must not wipe child-b's still-pending
    // request just because it happens to be the one lead.waitingAgentId currently tracks.
    const approved = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PostToolUse',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    expect(approved?.payload.state).toBe('waiting')

    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('waiting')
  })

  it('clears an earlier-requesting child pending id even after a later sibling displaces it from waitingAgentId (#10997 child-agent gap, round 2)', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    // child-a requests first (becomes waitingAgentId), child-b requests second and overwrites
    // waitingAgentId to child-b - child-a is now "displaced" but still has a pending entry.
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-b',
      tool_name: 'Bash',
      tool_input: { command: 'pwd' }
    })

    // child-a's own approval-granted tool run resolves ITS pending request, even though it no
    // longer equals lead.waitingAgentId (that single slot now points at child-b).
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PostToolUse',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    // child-b resolves too.
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionDenied',
      agent_id: 'child-b',
      tool_name: 'Bash',
      tool_input: { command: 'pwd' },
      reason: 'user_denied'
    })

    // Why: both children finish (not just resolve their permission) so resolveClaudePaneState's
    // separate, pre-existing "roster still has a working child" override doesn't independently
    // keep the pane at 'working' - unrelated to this fix, isolates what it actually tests.
    normalizeAndAccept(state, 'claude', { hook_event_name: 'SubagentStop', agent_id: 'child-a' })
    normalizeAndAccept(state, 'claude', { hook_event_name: 'SubagentStop', agent_id: 'child-b' })

    // Both cleared - the pane must not be wedged at 'waiting' forever.
    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('done')
  })

  it('un-wedges the lead once the tracked wait owner is no longer the last pending agent, in either resolution order (#10997, round 5)', () => {
    // Reverse of the "displaced earlier requester" test above: here the LATER requester
    // (still tracked as waitingAgentId) resolves FIRST, and the EARLIER requester (no longer
    // tracked) resolves LAST - the exact ordering both independent reviewers found still broken
    // after round 4 (gate 1's restore only fired when subagentOriginId === waitingAgentId).
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      agent_id: 'child-b',
      tool_name: 'Bash',
      tool_input: { command: 'pwd' }
    })

    // child-b (the tracked waitingAgentId) resolves first via its own approval-granted tool run.
    const bResolved = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PostToolUse',
      agent_id: 'child-b',
      tool_name: 'Bash',
      tool_input: { command: 'pwd' }
    })
    expect(bResolved?.payload.state).toBe('waiting')

    // child-a (displaced from waitingAgentId, never re-tracked) resolves last - this is the one
    // that used to stay wedged at 'waiting' forever.
    const aResolved = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PostToolUse',
      agent_id: 'child-a',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    expect(aResolved?.payload.state).toBe('working')

    // Why: both children finish (not just resolve their permission) so resolveClaudePaneState's
    // separate, pre-existing "roster still has a working child" override doesn't independently
    // keep the pane at 'working' - unrelated to this fix, isolates what it actually tests.
    normalizeAndAccept(state, 'claude', { hook_event_name: 'SubagentStop', agent_id: 'child-a' })
    normalizeAndAccept(state, 'claude', { hook_event_name: 'SubagentStop', agent_id: 'child-b' })

    const event = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })
    expect(event?.payload.state).toBe('done')
  })

  it('lets an interrupt still win over a pending PermissionRequest on Claude Stop', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'do it' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PermissionRequest',
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /' }
    })

    const event = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'Stop',
      is_interrupt: true
    })
    expect(event?.payload.state).toBe('done')
    expect(event?.payload.interrupted).toBe(true)
  })

  it('normalizes a Claude-compatible StopFailure to done without copying provider error text', () => {
    normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: { hook_event_name: 'UserPromptSubmit', prompt: 'say hi' }
      },
      'production'
    )

    const event = normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: {
          hook_event_name: 'StopFailure',
          error: 'invalid_request',
          error_details: 'model is not supported',
          last_assistant_message: 'API Error: model is not supported'
        }
      },
      'production'
    )

    expect(event?.payload).toMatchObject({
      state: 'done',
      prompt: 'say hi',
      agentType: 'claude'
    })
    expect(event?.payload.lastAssistantMessage).toBeUndefined()
  })

  it('maps Claude SessionStart to an idle done row so a resumed session earns its sidebar row before the first prompt', () => {
    const event = normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: {
          hook_event_name: 'SessionStart',
          source: 'resume',
          session_id: '44444444-4444-4444-8444-444444444444'
        }
      },
      'production'
    )

    // Why: 'working' would show a phantom spinner on an idle TUI; a session-boundary
    // 'done' renders the row idle, which is the truth at SessionStart.
    expect(event?.payload).toMatchObject({
      state: 'done',
      prompt: '',
      agentType: 'claude',
      sessionBoundary: true
    })
    expect(event?.payload.interrupted).toBeUndefined()
    expect(event?.hookEventName).toBe('SessionStart')
    // Why: SessionStart carries resume identity, so in-app resume works before any prompt.
    expect(event?.providerSession).toMatchObject({
      key: 'session_id',
      id: '44444444-4444-4444-8444-444444444444'
    })
  })

  it('resets stale Claude turn state when SessionStart announces a new session on the pane', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'fix bug' })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PreToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'ls' }
    })
    normalizeAndAccept(state, 'claude', { hook_event_name: 'SubagentStart', agent_id: 'agent-1' })

    const event = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'SessionStart',
      source: 'startup'
    })

    // Why: a new process owns the pane; stale prompt/tool/children must not survive
    // into the fresh session's idle row or gate it back up to 'working'.
    expect(event?.payload.state).toBe('done')
    expect(event?.payload.prompt).toBe('')
    expect(event?.payload.toolName).toBeUndefined()
    expect(event?.payload.subagents).toBeUndefined()
  })

  it('keeps the running Claude turn when SessionStart comes from a compact restart or a child session', () => {
    normalizeAndAccept(state, 'claude', { hook_event_name: 'UserPromptSubmit', prompt: 'say hi' })

    const compacted = normalizeHookPayload(
      state,
      'claude',
      { paneKey: PANE_KEY, payload: { hook_event_name: 'SessionStart', source: 'compact' } },
      'production'
    )
    // Why: unknown/missing sources fail closed — only startup/resume/clear are idle boundaries.
    const unknownSource = normalizeHookPayload(
      state,
      'claude',
      { paneKey: PANE_KEY, payload: { hook_event_name: 'SessionStart' } },
      'production'
    )
    const child = normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: { hook_event_name: 'SessionStart', source: 'startup', agent_id: 'agent-7' }
      },
      'production'
    )
    const stopped = normalizeAndAccept(state, 'claude', { hook_event_name: 'Stop' })

    // Why: auto-compact restarts mid-turn (PreCompact/PostCompact own that lifecycle) and a
    // child-attributed SessionStart must not flip the lead's live turn to an idle row.
    expect(compacted).toBeNull()
    expect(unknownSource).toBeNull()
    expect(child).toBeNull()
    expect(stopped?.payload).toMatchObject({ state: 'done', prompt: 'say hi' })
    expect(stopped?.payload.sessionBoundary).toBeUndefined()
  })

  it('rejects oversized paneKey', () => {
    const event = normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: 'x'.repeat(300),
        payload: { hook_event_name: 'UserPromptSubmit', prompt: 'hi' }
      },
      'production'
    )
    expect(event).toBeNull()
  })

  it('resumes work for task notifications without replacing the cached prompt', () => {
    normalizeHookPayload(
      state,
      'claude',
      { paneKey: PANE_KEY, payload: { hook_event_name: 'UserPromptSubmit', prompt: 'fix login' } },
      'production'
    )
    const event = normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: {
          hook_event_name: 'UserPromptSubmit',
          prompt: '<task-notification> <task-id>bzthj2b8r</task-id> <tool-use-id>t1</tool-use-id>'
        }
      },
      'production'
    )
    expect(event).not.toBeNull()
    expect(event!.payload.state).toBe('working')
    expect(event!.payload.prompt).toBe('fix login')
    expect(event!.hasExplicitPrompt).toBe(false)
  })

  it('emits a harness-injected UserPromptSubmit with an empty uncached prompt', () => {
    const event = normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: {
          hook_event_name: 'UserPromptSubmit',
          prompt: '<system-reminder>background context</system-reminder>'
        }
      },
      'production'
    )
    expect(event).not.toBeNull()
    expect(event!.payload.state).toBe('working')
    expect(event!.payload.prompt).toBe('')
    expect(event!.hasExplicitPrompt).toBe(false)
  })

  it('does not leave working after a compact-summary UserPromptSubmit (issue #11352)', () => {
    // Live repro: after /compact Claude injects "This session is being continued…" with no Stop.
    const event = normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: {
          hook_event_name: 'UserPromptSubmit',
          prompt:
            'This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.'
        }
      },
      'production'
    )
    expect(event).toBeNull()
  })

  it('maps an identity-matched Claude manual compact lifecycle', () => {
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'UserPromptSubmit',
      prompt: 'work before compact',
      prompt_id: CLAUDE_PREVIOUS_PROMPT_ID,
      session_id: 'session-a'
    })
    const pre = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PreCompact',
      trigger: 'manual',
      prompt_id: CLAUDE_PROMPT_ID,
      session_id: 'session-a'
    })
    expect(pre).not.toBeNull()
    expect(pre!.payload.state).toBe('working')
    expect(pre!.payload.agentType).toBe('claude')

    const post = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PostCompact',
      trigger: 'manual',
      prompt_id: CLAUDE_PROMPT_ID,
      session_id: 'session-a'
    })
    expect(post).not.toBeNull()
    expect(post!.payload.state).toBe('done')
    expect(post!.payload.agentType).toBe('claude')
  })

  it('keeps the preceding user prompt on the completed compact row', () => {
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'UserPromptSubmit',
      prompt: 'work before compact',
      prompt_id: CLAUDE_PREVIOUS_PROMPT_ID,
      session_id: 'session-a'
    })
    normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PreCompact',
      trigger: 'manual',
      prompt_id: CLAUDE_PROMPT_ID,
      session_id: 'session-a'
    })
    const post = normalizeAndAccept(state, 'claude', {
      hook_event_name: 'PostCompact',
      trigger: 'manual',
      prompt_id: CLAUDE_PROMPT_ID,
      session_id: 'session-a'
    })
    expect(post).not.toBeNull()
    expect(post!.payload.state).toBe('done')
    expect(post!.payload.prompt).toBe('work before compact')
  })

  it('treats a custom-element paste as an explicit user turn, not machinery', () => {
    normalizeHookPayload(
      state,
      'claude',
      { paneKey: PANE_KEY, payload: { hook_event_name: 'UserPromptSubmit', prompt: 'fix login' } },
      'production'
    )
    // Why: a real prompt starting with an unknown kebab tag (<my-custom-element>)
    // is the user's turn — it must reset the cached prompt and count as explicit,
    // so interrupt recovery does not leave the pane visibly done.
    const event = normalizeHookPayload(
      state,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: {
          hook_event_name: 'UserPromptSubmit',
          prompt: '<my-custom-element> render this component'
        }
      },
      'production'
    )
    expect(event).not.toBeNull()
    expect(event!.payload.prompt).toBe('<my-custom-element> render this component')
    expect(event!.hasExplicitPrompt).toBe(true)
  })

  it('treats a Grok user_query prompt as an explicit user turn', () => {
    const event = normalizeHookPayload(
      state,
      'grok',
      {
        paneKey: PANE_KEY,
        payload: {
          hookEventName: 'user_prompt_submit',
          prompt: '<user_query>fix the bug</user_query>'
        }
      },
      'production'
    )
    expect(event).not.toBeNull()
    // Grok wraps the real typed prompt; the envelope is stripped but it stays explicit.
    expect(event!.payload.prompt).toBe('fix the bug')
    expect(event!.hasExplicitPrompt).toBe(true)
  })

  it('isolates caches between listener instances', () => {
    const a = createHookListenerState()
    const b = createHookListenerState()
    normalizeHookPayload(
      a,
      'claude',
      { paneKey: PANE_KEY, payload: { hook_event_name: 'UserPromptSubmit', prompt: 'first' } },
      'production'
    )
    // The second listener has no cached prompt for this paneKey, so a tool
    // event without a fresh prompt should produce empty prompt string.
    const event = normalizeHookPayload(
      b,
      'claude',
      {
        paneKey: PANE_KEY,
        payload: {
          hook_event_name: 'PreToolUse',
          tool_name: 'Read',
          tool_input: { file_path: '/etc/hosts' }
        }
      },
      'production'
    )
    expect(event).not.toBeNull()
    expect(event!.payload.prompt).toBe('')
  })

  it('bounds Amp thread-scoped caches for a long-lived pane', () => {
    let latestPrompt = ''
    for (let i = 0; i < 40; i++) {
      const threadId = `thread-${i}`
      const started = normalizeHookPayload(
        state,
        'amp',
        {
          paneKey: PANE_KEY,
          payload: {
            hookEventName: 'agent.start',
            threadId,
            message: `prompt ${i}`
          }
        },
        'production'
      )
      expect(started?.payload.state).toBe('working')

      const ended = normalizeHookPayload(
        state,
        'amp',
        {
          paneKey: PANE_KEY,
          payload: {
            hookEventName: 'agent.end',
            threadId,
            status: 'completed'
          }
        },
        'production'
      )
      expect(ended?.payload.state).toBe('done')
      latestPrompt = ended?.payload.prompt ?? ''
    }

    const scopedPrefix = `${PANE_KEY}\0amp:`
    const promptKeys = [...state.lastPromptByPaneKey.keys()].filter((key) =>
      key.startsWith(scopedPrefix)
    )
    const toolKeys = [...state.lastToolByPaneKey.keys()].filter((key) =>
      key.startsWith(scopedPrefix)
    )
    const completedKeys = [...state.ampCompletedCacheKeys].filter((key) =>
      key.startsWith(scopedPrefix)
    )

    expect(promptKeys.length).toBeLessThanOrEqual(32)
    expect(toolKeys.length).toBeLessThanOrEqual(32)
    expect(completedKeys.length).toBeLessThanOrEqual(32)
    expect(state.lastPromptByPaneKey.has(`${scopedPrefix}thread-0`)).toBe(false)
    expect(state.lastPromptByPaneKey.get(`${scopedPrefix}thread-39`)).toBe('prompt 39')
    expect(latestPrompt).toBe('prompt 39')
  })
})
