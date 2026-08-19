import { afterEach, describe, expect, it } from 'vitest'
import { Coordinator } from './coordinator'
import type { CoordinatorRuntime } from './coordinator-runtime-contract'
import { OrchestrationDb } from './db'

// Why: split out of coordinator.test.ts to keep that file under the max-lines budget.
describe('Coordinator stale dispatch escalation', () => {
  let db: OrchestrationDb

  afterEach(() => {
    db?.close()
  })

  it('escalates a stale dispatch once (#14829) without failing it, and the coordinator sees it', async () => {
    db = new OrchestrationDb(':memory:')
    const runtime: CoordinatorRuntime = {
      async sendTerminalAgentPrompt(handle, prompt) {
        return { handle, text: prompt, accepted: true }
      },
      async listTerminals() {
        return { terminals: [] }
      },
      async createTerminal() {
        throw new Error('unexpected terminal create')
      },
      async waitForTerminal(handle) {
        return { handle, condition: 'exit' }
      },
      async probeWorktreeDrift() {
        return null
      }
    }
    const task = db.createTask({ spec: 'work' })
    const ctx = db.createDispatchContext(task.id, 'term_stale')

    const sqlite = (
      db as unknown as { db: { prepare: (s: string) => { run: (...a: unknown[]) => void } } }
    ).db
    const iso = (ms: number) => new Date(Date.now() - ms).toISOString()
    sqlite
      .prepare('UPDATE dispatch_contexts SET dispatched_at = ?, last_heartbeat_at = ? WHERE id = ?')
      .run(iso(60 * 60 * 1000), iso(30 * 60 * 1000), ctx.id)

    const coordinator = new Coordinator(db, runtime, {
      spec: 'go',
      coordinatorHandle: 'coord',
      pollIntervalMs: 20
    })

    // Several ticks: one to insert the escalation, more to read it back and
    // prove a second tick past the same stale spell doesn't insert another.
    const runPromise = coordinator.run()
    await new Promise((r) => {
      setTimeout(r, 120)
    })
    coordinator.stop()
    const result = await runPromise

    const escalationRows = (
      db as unknown as {
        db: { prepare: (s: string) => { all: (...a: unknown[]) => unknown[] } }
      }
    ).db
      .prepare("SELECT * FROM messages WHERE type = 'escalation'")
      .all() as { subject: string; payload: string | null }[]
    expect(escalationRows).toHaveLength(1)
    expect(escalationRows[0].subject).toMatch(/has not reported/)
    expect(JSON.parse(escalationRows[0].payload ?? '{}')).toMatchObject({ taskId: task.id })

    // Fires exactly once per stale spell.
    expect(db.getDispatchContext(task.id)?.stale_escalated_at).toBeTruthy()
    // Not auto-failed/auto-completed from staleness alone — the payload omits
    // dispatchId (mirroring failActiveDispatchOnExit) so applyEscalationToDispatch
    // can only observe it, never act on it.
    expect(db.getTask(task.id)?.status).toBe('dispatched')
    expect(result.escalations.some((m) => m.type === 'escalation')).toBe(true)
  })
})
