import { afterEach, describe, expect, it } from 'vitest'
import { evaluateDagConvergence } from './coordinator-dag-convergence'
import { OrchestrationDb } from './db'

describe('evaluateDagConvergence', () => {
  let db: OrchestrationDb

  afterEach(() => {
    db?.close()
  })

  it('reports all-done once a remaining Task is cancelled or superseded (#14548)', () => {
    db = new OrchestrationDb(':memory:')
    const done = db.createTask({ spec: 'finished work' })
    db.updateTaskStatus(done.id, 'completed', 'ok')
    const cancelled = db.createTask({ spec: 'cut scope' })
    db.cancelTask(cancelled.id, 'cancelled', { reason: 'no longer needed' })

    expect(evaluateDagConvergence(db, () => {})).toBe('all-done')
  })

  it('stays active while a ready Task remains, cancelled Tasks aside', () => {
    db = new OrchestrationDb(':memory:')
    const cancelled = db.createTask({ spec: 'cut scope' })
    db.cancelTask(cancelled.id, 'cancelled', { reason: 'no longer needed' })
    db.createTask({ spec: 'still pending work' })

    expect(evaluateDagConvergence(db, () => {})).toBe('active')
  })

  it('converges once a pending dependent is cascade-cancelled with its dependency, not stranded (#14548)', () => {
    // Why: without the cascade, the dependent would stay 'pending' forever — counted as
    // "active" here — so the Run would never converge and evaluateDagConvergence's own
    // stuck-DAG warning (which only fires when active.length === 0) would never fire either.
    db = new OrchestrationDb(':memory:')
    const dependency = db.createTask({ spec: 'cut scope' })
    db.createTask({ spec: 'waits on cut scope', deps: [dependency.id] })
    db.cancelTask(dependency.id, 'cancelled', { reason: 'no longer needed' })

    expect(evaluateDagConvergence(db, () => {})).toBe('all-done')
  })
})
