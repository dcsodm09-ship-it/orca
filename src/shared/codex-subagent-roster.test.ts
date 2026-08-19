import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  AGENT_MODEL_MAX_LENGTH,
  AGENT_STATUS_MAX_SUBAGENTS,
  AGENT_STATUS_STALE_AFTER_MS,
  AGENT_TYPE_MAX_LENGTH
} from './agent-status-types'
import {
  codexRosterToSnapshots,
  finishCodexSubagent,
  hasHookConfirmedCodexSubagent,
  isHookConfirmedCodexSubagent,
  retireStaleHookConfirmedCodexSubagents,
  setCodexSubagentModel,
  upsertCodexSubagent,
  type CodexSubagentRoster
} from './codex-subagent-roster'

describe('Codex subagent roster', () => {
  it('normalizes retained identity fields before storing them', () => {
    const roster: CodexSubagentRoster = new Map()

    upsertCodexSubagent(
      roster,
      ' child-1 ',
      {
        agentType: `reviewer\n${'x'.repeat(AGENT_TYPE_MAX_LENGTH * 2)}`,
        description: 'Review the sidebar lifecycle',
        model: `gpt-model-${'x'.repeat(AGENT_MODEL_MAX_LENGTH * 2)}`,
        state: 'working'
      },
      10
    )

    const snapshot = codexRosterToSnapshots(roster)?.[0]
    expect([...roster.keys()]).toEqual(['child-1'])
    expect(snapshot?.agentType).toHaveLength(AGENT_TYPE_MAX_LENGTH)
    expect(snapshot?.agentType).not.toContain('\n')
    expect(snapshot?.description).toBe('Review the sidebar lifecycle')
    expect(snapshot?.model).toHaveLength(AGENT_MODEL_MAX_LENGTH)

    finishCodexSubagent(roster, ' child-1 ')
    expect(roster.size).toBe(0)
  })

  it('rejects an id that would normalize to an invisible child', () => {
    const roster: CodexSubagentRoster = new Map()

    upsertCodexSubagent(roster, '   ', { state: 'waiting' }, 10)

    expect(roster.size).toBe(0)
  })

  it('bounds live storage while admitting a replacement after one child stops', () => {
    const roster: CodexSubagentRoster = new Map()
    for (let index = 0; index <= AGENT_STATUS_MAX_SUBAGENTS; index += 1) {
      upsertCodexSubagent(roster, `child-${index}`, { state: 'working' }, index)
    }

    expect(roster.size).toBe(AGENT_STATUS_MAX_SUBAGENTS)
    expect(roster.has(`child-${AGENT_STATUS_MAX_SUBAGENTS}`)).toBe(false)

    finishCodexSubagent(roster, 'child-0')
    upsertCodexSubagent(roster, 'replacement', { state: 'working' }, 100)

    expect(roster.size).toBe(AGENT_STATUS_MAX_SUBAGENTS)
    expect(roster.has('replacement')).toBe(true)
  })

  describe('setCodexSubagentModel', () => {
    it('records the model without disturbing the child lifecycle or label', () => {
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'child-1', { description: '/root/audit', state: 'waiting' }, 10)

      setCodexSubagentModel(roster, 'child-1', ' gpt-5.6-terra ')

      expect(codexRosterToSnapshots(roster)).toEqual([
        {
          id: 'child-1',
          agentType: undefined,
          description: '/root/audit',
          model: 'gpt-5.6-terra',
          state: 'waiting',
          startedAt: 10
        }
      ])
    })

    it('never creates a row for a child that is no longer tracked', () => {
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'child-1', { state: 'working' }, 10)
      finishCodexSubagent(roster, 'child-1')

      // A model read racing a completed child must not resurrect its row.
      setCodexSubagentModel(roster, 'child-1', 'gpt-5.6-terra')

      expect(roster.size).toBe(0)
    })

    it('keeps a known model when the new value is empty', () => {
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'child-1', { model: 'gpt-5.6-sol', state: 'working' }, 10)

      setCodexSubagentModel(roster, 'child-1', '   ')
      setCodexSubagentModel(roster, 'child-1', undefined)

      expect(roster.get('child-1')?.model).toBe('gpt-5.6-sol')
    })

    it('bounds an oversized model to the shared cap', () => {
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'child-1', { state: 'working' }, 10)

      setCodexSubagentModel(roster, 'child-1', 'x'.repeat(AGENT_MODEL_MAX_LENGTH + 50))

      expect(roster.get('child-1')?.model).toHaveLength(AGENT_MODEL_MAX_LENGTH)
    })
  })

  describe('retireStaleHookConfirmedCodexSubagents', () => {
    afterEach(() => {
      vi.useRealTimers()
    })

    it('eventually retires a hook-confirmed row whose own SubagentStop never arrives', () => {
      // Why: a 'hook' row is otherwise exempt from the lead Stop's roster wipe entirely (see
      // hasHookConfirmedCodexSubagent) — without a bounded fallback, a lost child SubagentStop
      // (a known Codex CLI gap) would pin the row, and the pane, forever.
      vi.useFakeTimers()
      vi.setSystemTime(0)
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'child-1', { state: 'working', source: 'hook' }, 10)

      expect(retireStaleHookConfirmedCodexSubagents(roster, AGENT_STATUS_STALE_AFTER_MS)).toEqual(
        []
      )
      expect(roster.has('child-1')).toBe(true)

      expect(
        retireStaleHookConfirmedCodexSubagents(roster, AGENT_STATUS_STALE_AFTER_MS + 1)
      ).toEqual(['child-1'])
      expect(roster.has('child-1')).toBe(false)
    })

    it('is immune to the same Stop body being re-normalized many times within the same window', () => {
      // Why: AgentHookServer's scheduleCodexSubagentPoll re-normalizes the same saved hook body
      // on a 1s timer while a transcript child is unresolved, calling this function repeatedly
      // for what is really one real Stop. A wall-clock bound must not evict a genuinely-alive
      // child just because it was called many times in a short window — a per-call counter did.
      vi.useFakeTimers()
      vi.setSystemTime(0)
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'child-1', { state: 'working', source: 'hook' }, 10)

      for (let i = 0; i < 20; i++) {
        vi.advanceTimersByTime(1_000)
        expect(retireStaleHookConfirmedCodexSubagents(roster, Date.now())).toEqual([])
      }
      expect(roster.has('child-1')).toBe(true)
    })

    it('resets the staleness window on a fresh hook touch', () => {
      vi.useFakeTimers()
      vi.setSystemTime(0)
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'child-1', { state: 'working', source: 'hook' }, 10)

      vi.advanceTimersByTime(AGENT_STATUS_STALE_AFTER_MS - 1)
      // Why: a fresh live confirmation proves the child is genuinely still there — it must
      // reset the window, not merely delay retirement by the same fixed amount.
      upsertCodexSubagent(roster, 'child-1', { state: 'working', source: 'hook' }, 20)
      vi.advanceTimersByTime(AGENT_STATUS_STALE_AFTER_MS - 1)

      expect(retireStaleHookConfirmedCodexSubagents(roster, Date.now())).toEqual([])
      expect(roster.has('child-1')).toBe(true)
    })

    it('never touches a transcript-only row', () => {
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'child-1', { state: 'working', source: 'transcript' }, 10)

      expect(
        retireStaleHookConfirmedCodexSubagents(
          roster,
          Date.now() + AGENT_STATUS_STALE_AFTER_MS * 10
        )
      ).toEqual([])
      expect(roster.has('child-1')).toBe(true)
    })
  })

  describe('hasHookConfirmedCodexSubagent / isHookConfirmedCodexSubagent', () => {
    it('distinguishes a hook-confirmed row from a transcript-only one', () => {
      const roster: CodexSubagentRoster = new Map()
      upsertCodexSubagent(roster, 'hook-child', { state: 'working', source: 'hook' }, 10)
      upsertCodexSubagent(
        roster,
        'transcript-child',
        { state: 'working', source: 'transcript' },
        10
      )

      expect(hasHookConfirmedCodexSubagent(roster)).toBe(true)
      expect(isHookConfirmedCodexSubagent(roster, 'hook-child')).toBe(true)
      expect(isHookConfirmedCodexSubagent(roster, 'transcript-child')).toBe(false)
      expect(isHookConfirmedCodexSubagent(roster, 'missing-child')).toBe(false)
      expect(isHookConfirmedCodexSubagent(undefined, 'hook-child')).toBe(false)
    })
  })
})
