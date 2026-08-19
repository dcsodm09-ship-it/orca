import { afterEach, describe, expect, it, vi } from 'vitest'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import type * as NodeFs from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// Why: ESM namespaces are not configurable, so counting rollout opens needs a
// delegating mock rather than vi.spyOn. Every other export stays real.
const openSyncCalls = vi.hoisted(() => vi.fn())
vi.mock('node:fs', async (importOriginal) => {
  const actual = await importOriginal<typeof NodeFs>()
  return {
    ...actual,
    openSync: (...args: Parameters<typeof actual.openSync>) => {
      openSyncCalls(...args)
      return actual.openSync(...args)
    }
  }
})

import {
  createCodexSubagentTranscriptState,
  hasTrackedCodexTranscriptSubagents,
  reconcileCodexSubagentTranscript,
  retireExpiredUnresolvedCodexSubagentTranscripts
} from './codex-subagent-transcript'
import {
  codexRosterToSnapshots,
  retireStaleHookConfirmedCodexSubagents,
  upsertCodexSubagent,
  type CodexSubagentRoster
} from './codex-subagent-roster'

const CHILD_ID = '019fa65f-3144-7151-9c02-cff7a28f316f'

function jsonl(records: unknown[]): string {
  return `${records.map((record) => JSON.stringify(record)).join('\n')}\n`
}

function activity(kind: string, occurredAtMs = 1234): unknown {
  return {
    type: 'event_msg',
    payload: {
      type: 'sub_agent_activity',
      occurred_at_ms: occurredAtMs,
      agent_thread_id: CHILD_ID,
      agent_path: '/root/sidebar_repro',
      kind
    }
  }
}

/** `<root>/YYYY/MM/DD` for a timestamp, matching how Codex buckets rollouts by local start date. */
function dayDirectory(root: string, atMs: number): string {
  const at = new Date(atMs)
  const pad = (value: number): string => String(value).padStart(2, '0')
  return join(root, String(at.getFullYear()), pad(at.getMonth() + 1), pad(at.getDate()))
}

describe('Codex subagent transcript reconciliation', () => {
  const dirs: string[] = []

  afterEach(() => {
    for (const dir of dirs) {
      rmSync(dir, { recursive: true, force: true })
    }
    dirs.length = 0
  })

  it('adds a child from the parent rollout and removes it after task completion', () => {
    const dir = mkdtempSync(join(tmpdir(), 'codex-subagent-transcript-'))
    dirs.push(dir)
    const parentPath = join(dir, 'rollout-parent.jsonl')
    const childPath = join(dir, `rollout-child-${CHILD_ID}.jsonl`)
    writeFileSync(parentPath, jsonl([activity('started')]))
    writeFileSync(childPath, jsonl([{ type: 'event_msg', payload: { type: 'task_started' } }]))
    const state = createCodexSubagentTranscriptState()
    const roster: CodexSubagentRoster = new Map()

    reconcileCodexSubagentTranscript(state, roster, parentPath)

    expect(hasTrackedCodexTranscriptSubagents(state)).toBe(true)
    expect(codexRosterToSnapshots(roster)).toEqual([
      {
        id: CHILD_ID,
        description: '/root/sidebar_repro',
        state: 'working',
        startedAt: 1234,
        agentType: undefined,
        model: undefined
      }
    ])

    writeFileSync(
      childPath,
      jsonl([
        { type: 'event_msg', payload: { type: 'task_started' } },
        { type: 'event_msg', payload: { type: 'task_complete' } }
      ])
    )
    reconcileCodexSubagentTranscript(state, roster, parentPath)

    expect(hasTrackedCodexTranscriptSubagents(state)).toBe(false)
    expect(codexRosterToSnapshots(roster)).toBeUndefined()
  })

  it('resolves a child rollout filed under a later session day than the parent', () => {
    const root = mkdtempSync(join(tmpdir(), 'codex-subagent-transcript-'))
    dirs.push(root)
    const childStartedAt = Date.now()
    const parentDir = dayDirectory(root, childStartedAt - 24 * 60 * 60 * 1000)
    const childDir = dayDirectory(root, childStartedAt)
    mkdirSync(parentDir, { recursive: true })
    mkdirSync(childDir, { recursive: true })
    const parentPath = join(parentDir, 'rollout-parent.jsonl')
    const childPath = join(childDir, `rollout-child-${CHILD_ID}.jsonl`)
    writeFileSync(parentPath, jsonl([activity('started', childStartedAt)]))
    writeFileSync(childPath, jsonl([{ type: 'event_msg', payload: { type: 'task_started' } }]))
    const state = createCodexSubagentTranscriptState()
    const roster: CodexSubagentRoster = new Map()

    reconcileCodexSubagentTranscript(state, roster, parentPath)
    writeFileSync(
      childPath,
      jsonl([
        { type: 'event_msg', payload: { type: 'task_started' } },
        { type: 'event_msg', payload: { type: 'task_complete' } }
      ])
    )
    reconcileCodexSubagentTranscript(state, roster, parentPath)

    // Why: only a cross-day lookup can observe the completion; the parent-directory scan never finds this file.
    expect(roster.size).toBe(0)
    expect(hasTrackedCodexTranscriptSubagents(state)).toBe(false)
  })

  it('retires a child whose rollout never becomes readable', () => {
    vi.useFakeTimers()
    try {
      const dir = mkdtempSync(join(tmpdir(), 'codex-subagent-transcript-'))
      dirs.push(dir)
      const parentPath = join(dir, 'rollout-parent.jsonl')
      writeFileSync(parentPath, jsonl([activity('started')]))
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()

      reconcileCodexSubagentTranscript(state, roster, parentPath)
      expect(roster.size).toBe(1)

      // Why: within the grace window a slow-to-appear rollout must not drop a live child.
      vi.advanceTimersByTime(30_000)
      reconcileCodexSubagentTranscript(state, roster, parentPath)
      expect(roster.size).toBe(1)

      vi.advanceTimersByTime(31_000)
      reconcileCodexSubagentTranscript(state, roster, parentPath)
      expect(roster.size).toBe(0)
      expect(hasTrackedCodexTranscriptSubagents(state)).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not delete a row a live hook confirmed while its own rollout stayed unreadable', () => {
    // Why: this id can be discovered via transcript polling first, then separately confirmed
    // by a live hook (upgrading its roster row to source:'hook') before the transcript side's
    // own grace window for THIS id expires. This grace-expiry delete is a second, independent
    // call site from retireExpiredUnresolvedCodexSubagentTranscripts (reachable on every
    // reconcile, not just at Stop) and needs the identical hook-confirmed guard.
    vi.useFakeTimers()
    try {
      const dir = mkdtempSync(join(tmpdir(), 'codex-subagent-transcript-'))
      dirs.push(dir)
      const parentPath = join(dir, 'rollout-parent.jsonl')
      writeFileSync(parentPath, jsonl([activity('started')]))
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()

      reconcileCodexSubagentTranscript(state, roster, parentPath)
      expect(roster.size).toBe(1)

      // A live hook event (e.g. an agent_id-carrying PreToolUse) confirms the same id directly.
      upsertCodexSubagent(roster, CHILD_ID, { state: 'working', source: 'hook' }, Date.now())
      expect(roster.get(CHILD_ID)?.source).toBe('hook')

      vi.advanceTimersByTime(61_000)
      reconcileCodexSubagentTranscript(state, roster, parentPath)

      expect(roster.has(CHILD_ID)).toBe(true)
      expect(hasTrackedCodexTranscriptSubagents(state)).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps refreshing a hook-confirmed row via genuinely new transcript activity', () => {
    // Why: a hook-confirmed child only ever reconfirmed by transcript polling (no further hook
    // events touching it directly) must not go stale and be retired by
    // retireStaleHookConfirmedCodexSubagents just because nothing but reconcile ever touches it
    // — repeatedly observing genuinely NEW rollout content is direct evidence it's alive and
    // must reset the staleness clock the same way a fresh hook touch does.
    vi.useFakeTimers()
    try {
      const dir = mkdtempSync(join(tmpdir(), 'codex-subagent-transcript-'))
      dirs.push(dir)
      const parentPath = join(dir, 'rollout-parent.jsonl')
      const childPath = join(dir, `rollout-child-${CHILD_ID}.jsonl`)
      writeFileSync(parentPath, jsonl([activity('started')]))
      const childRecords: unknown[] = [{ type: 'event_msg', payload: { type: 'task_started' } }]
      writeFileSync(childPath, jsonl(childRecords))
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()

      reconcileCodexSubagentTranscript(state, roster, parentPath)
      upsertCodexSubagent(roster, CHILD_ID, { state: 'working', source: 'hook' }, Date.now())

      // 4 ticks of 10 minutes each = 40 minutes total elapsed, well past the 30-minute stale
      // bound overall — but each individual gap stays under it because each poll observes a
      // genuinely new line appended since the last one, resetting lastConfirmedAt.
      for (let i = 0; i < 4; i++) {
        vi.advanceTimersByTime(10 * 60_000)
        childRecords.push({ type: 'event_msg', payload: { type: 'agent_message' } })
        writeFileSync(childPath, jsonl(childRecords))
        reconcileCodexSubagentTranscript(state, roster, parentPath)
        expect(retireStaleHookConfirmedCodexSubagents(roster, Date.now())).toEqual([])
      }
      expect(roster.has(CHILD_ID)).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not treat a static, unchanged rollout as ongoing proof of life', () => {
    // Why: readJsonlCursor returns `[]` (not undefined) for a file that's readable but has not
    // grown since the last poll — a child that crashed right after writing task_started leaves
    // exactly this shape. Repeatedly reading that same static file successfully must NOT count
    // as fresh liveness evidence, or the 1s poll (scheduleCodexSubagentPoll) would keep such a
    // phantom alive forever by re-observing "still readable, still unchanged" every tick.
    vi.useFakeTimers()
    try {
      const dir = mkdtempSync(join(tmpdir(), 'codex-subagent-transcript-'))
      dirs.push(dir)
      const parentPath = join(dir, 'rollout-parent.jsonl')
      const childPath = join(dir, `rollout-child-${CHILD_ID}.jsonl`)
      writeFileSync(parentPath, jsonl([activity('started')]))
      writeFileSync(childPath, jsonl([{ type: 'event_msg', payload: { type: 'task_started' } }]))
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()

      reconcileCodexSubagentTranscript(state, roster, parentPath)
      upsertCodexSubagent(roster, CHILD_ID, { state: 'working', source: 'hook' }, Date.now())

      // The child's rollout is never touched again — every subsequent read sees the same
      // static, already-fully-read file (readJsonlCursor returns `[]`).
      for (let i = 0; i < 3; i++) {
        vi.advanceTimersByTime(10 * 60_000)
        reconcileCodexSubagentTranscript(state, roster, parentPath)
      }
      vi.advanceTimersByTime(1)
      expect(retireStaleHookConfirmedCodexSubagents(roster, Date.now())).toEqual([CHILD_ID])
      expect(roster.has(CHILD_ID)).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('removes a child when Codex reports it interrupted', () => {
    const dir = mkdtempSync(join(tmpdir(), 'codex-subagent-transcript-'))
    dirs.push(dir)
    const parentPath = join(dir, 'rollout-parent.jsonl')
    writeFileSync(parentPath, jsonl([activity('started')]))
    const state = createCodexSubagentTranscriptState()
    const roster: CodexSubagentRoster = new Map()
    reconcileCodexSubagentTranscript(state, roster, parentPath)

    writeFileSync(parentPath, jsonl([activity('started'), activity('interrupted')]))
    reconcileCodexSubagentTranscript(state, roster, parentPath)

    expect(hasTrackedCodexTranscriptSubagents(state)).toBe(false)
    expect(roster.size).toBe(0)
  })

  describe('retireExpiredUnresolvedCodexSubagentTranscripts', () => {
    it('stops tracking but does not delete a row a live hook has since confirmed', () => {
      // Why: an id can be transcript-discovered first, then separately confirmed by a live
      // hook (upgrading its roster row to source:'hook') before its transcript-side grace
      // window expires. Deleting the roster row here would silently undo that real
      // confirmation and could report a genuinely still-running child 'done'.
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()
      state.subagents.set(CHILD_ID, { offset: 0, carry: '', startedAt: 10, unresolvedSince: 0 })
      upsertCodexSubagent(roster, CHILD_ID, { state: 'working', source: 'hook' }, 10)

      retireExpiredUnresolvedCodexSubagentTranscripts(state, roster, 61_000)

      expect(roster.has(CHILD_ID)).toBe(true)
      expect(state.subagents.has(CHILD_ID)).toBe(false)
    })

    it('still deletes a transcript-only row past the grace window', () => {
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()
      state.subagents.set(CHILD_ID, { offset: 0, carry: '', startedAt: 10, unresolvedSince: 0 })
      upsertCodexSubagent(roster, CHILD_ID, { state: 'working', source: 'transcript' }, 10)

      retireExpiredUnresolvedCodexSubagentTranscripts(state, roster, 61_000)

      expect(roster.has(CHILD_ID)).toBe(false)
      expect(state.subagents.has(CHILD_ID)).toBe(false)
    })

    it('leaves an unresolved row alone within the grace window', () => {
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()
      state.subagents.set(CHILD_ID, { offset: 0, carry: '', startedAt: 10, unresolvedSince: 0 })
      upsertCodexSubagent(roster, CHILD_ID, { state: 'working', source: 'transcript' }, 10)

      retireExpiredUnresolvedCodexSubagentTranscripts(state, roster, 59_000)

      expect(roster.has(CHILD_ID)).toBe(true)
      expect(state.subagents.has(CHILD_ID)).toBe(true)
    })
  })

  describe('child model identity', () => {
    function turnContext(model: string): unknown {
      return { type: 'turn_context', payload: { turn_id: 'turn-1', model } }
    }

    function started(): unknown {
      return { type: 'event_msg', payload: { type: 'task_started' } }
    }

    /** Parent rollout with one live child, plus that child's own rollout. */
    function seedPair(childRecords: unknown[]): {
      parentPath: string
      childPath: string
    } {
      const dir = mkdtempSync(join(tmpdir(), 'codex-subagent-model-'))
      dirs.push(dir)
      const parentPath = join(dir, 'rollout-parent.jsonl')
      const childPath = join(dir, `rollout-child-${CHILD_ID}.jsonl`)
      writeFileSync(parentPath, jsonl([turnContext('gpt-5.6-sol'), activity('started')]))
      writeFileSync(childPath, jsonl(childRecords))
      return { parentPath, childPath }
    }

    it('reports the model from the child rollout, not the parent model', () => {
      const { parentPath } = seedPair([started(), turnContext('gpt-5.6-terra')])
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()

      reconcileCodexSubagentTranscript(state, roster, parentPath)

      // The parent runs sol; only the child's own turn_context may set its model.
      expect(codexRosterToSnapshots(roster)?.[0]?.model).toBe('gpt-5.6-terra')
    })

    it('keeps the discovered model when a later poll carries no turn_context', () => {
      const { parentPath, childPath } = seedPair([started(), turnContext('gpt-5.6-terra')])
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()
      reconcileCodexSubagentTranscript(state, roster, parentPath)

      // The cursor is incremental: this appended line is all the next read sees.
      writeFileSync(
        childPath,
        jsonl([
          started(),
          turnContext('gpt-5.6-terra'),
          { type: 'event_msg', payload: { type: 'agent_message' } }
        ])
      )
      reconcileCodexSubagentTranscript(state, roster, parentPath)

      expect(codexRosterToSnapshots(roster)?.[0]?.model).toBe('gpt-5.6-terra')
    })

    it('tracks the newest model when the child switches mid-session', () => {
      const { parentPath } = seedPair([
        started(),
        turnContext('gpt-5.6-terra'),
        turnContext('gpt-5.6-sol')
      ])
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()

      reconcileCodexSubagentTranscript(state, roster, parentPath)

      expect(codexRosterToSnapshots(roster)?.[0]?.model).toBe('gpt-5.6-sol')
    })

    it('leaves the child working and keeps its identity while reading the model', () => {
      const { parentPath } = seedPair([started(), turnContext('gpt-5.6-terra')])
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()

      reconcileCodexSubagentTranscript(state, roster, parentPath)

      // Model discovery must not move lifecycle or overwrite the child's label.
      expect(codexRosterToSnapshots(roster)).toEqual([
        {
          id: CHILD_ID,
          description: '/root/sidebar_repro',
          state: 'working',
          startedAt: 1234,
          agentType: undefined,
          model: 'gpt-5.6-terra'
        }
      ])
    })

    it('does not resurrect a child that completed in the same read', () => {
      const { parentPath } = seedPair([
        started(),
        turnContext('gpt-5.6-terra'),
        { type: 'event_msg', payload: { type: 'task_complete' } }
      ])
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()

      reconcileCodexSubagentTranscript(state, roster, parentPath)

      expect(roster.size).toBe(0)
      expect(hasTrackedCodexTranscriptSubagents(state)).toBe(false)
    })

    it('reads the model without opening any file beyond the parent and child rollouts', () => {
      const { parentPath } = seedPair([started(), turnContext('gpt-5.6-terra')])
      const state = createCodexSubagentTranscriptState()
      const roster: CodexSubagentRoster = new Map()
      openSyncCalls.mockClear()

      reconcileCodexSubagentTranscript(state, roster, parentPath)

      // Why: model extraction reuses the records already read for completion
      // detection, so it must add no file I/O of its own.
      expect(openSyncCalls).toHaveBeenCalledTimes(2)
      expect(codexRosterToSnapshots(roster)?.[0]?.model).toBe('gpt-5.6-terra')
    })
  })
})
