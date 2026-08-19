import { afterEach, describe, expect, it } from 'vitest'
import { Coordinator } from './coordinator'
import type { CoordinatorRuntime } from './coordinator-runtime-contract'
import { OrchestrationDb } from './db'

// Why: split out of coordinator.test.ts to keep that file under the max-lines budget.
describe('Coordinator finalStatus with cancelled/superseded tasks (#14548)', () => {
  let db: OrchestrationDb

  afterEach(() => {
    db?.close()
  })

  function createNoopRuntime(): CoordinatorRuntime {
    return {
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
  }

  // Why: a deliberately cancelled/superseded task didn't complete the work - a Run containing
  // one must not report finalStatus 'completed' just because every task reached SOME terminal
  // status.
  it('reports failed when the only task was cancelled', async () => {
    db = new OrchestrationDb(':memory:')
    const task = db.createTask({ spec: 'no longer needed' })
    db.cancelTask(task.id, 'cancelled')
    const coordinator = new Coordinator(db, createNoopRuntime(), {
      spec: 'go',
      coordinatorHandle: 'coord',
      pollIntervalMs: 10
    })

    const result = await coordinator.run()

    expect(result.status).toBe('failed')
  })

  it('reports completed when a superseded task has a completed replacement', async () => {
    db = new OrchestrationDb(':memory:')
    const original = db.createTask({ spec: 'old approach' })
    const replacement = db.createTask({ spec: 'new approach' })
    db.updateTaskStatus(replacement.id, 'completed')
    db.cancelTask(original.id, 'superseded', { replacementTaskId: replacement.id })
    const coordinator = new Coordinator(db, createNoopRuntime(), {
      spec: 'go',
      coordinatorHandle: 'coord',
      pollIntervalMs: 10
    })

    const result = await coordinator.run()

    expect(result.status).toBe('completed')
  })
})
