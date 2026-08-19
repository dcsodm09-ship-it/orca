import { afterEach, describe, expect, it } from 'vitest'
import { OrchestrationDb } from './db'

// Why (#14809): a --to-only dispatch commits status='dispatched' and claims the terminal's
// one active-dispatch slot without ever touching it; findReusableUninjectedDispatchContext
// is what lets an --inject retry on the identical task+terminal deliver instead of stalling.
describe('findReusableUninjectedDispatchContext', () => {
  let db: OrchestrationDb | undefined

  afterEach(() => {
    db?.close()
  })

  function createDb(): OrchestrationDb {
    db = new OrchestrationDb(':memory:')
    return db
  }

  it('returns an un-injected context for the identical task+terminal', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_worker')

    expect(d.findReusableUninjectedDispatchContext(task.id, 'term_worker')?.id).toBe(ctx.id)
  })

  it('ignores a context whose capability was already minted', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_worker')
    d.mintDispatchCapability({
      dispatchId: ctx.id,
      paneKey: 'tab_worker:leaf_worker',
      processIncarnation: 'worker:1'
    })

    expect(d.findReusableUninjectedDispatchContext(task.id, 'term_worker')).toBeUndefined()
  })

  it('ignores a context for a different task', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const other = d.createTask({ spec: 'other work' })
    d.createDispatchContext(task.id, 'term_worker')

    expect(d.findReusableUninjectedDispatchContext(other.id, 'term_worker')).toBeUndefined()
  })

  it('reuses onto the current pane when nothing else claims it', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_worker')

    expect(
      d.findReusableUninjectedDispatchContext(task.id, 'term_worker', 'tab_worker:leaf_worker')?.id
    ).toBe(ctx.id)
  })

  // Why (#14809): mintDispatchCapability unconditionally rebinds the reused row's
  // assignee_pane_key to whatever is passed here — a stale context must not be reusable onto a
  // pane another active Dispatch already owns, or two active Dispatches end up sharing one pane.
  it('refuses reuse when the terminal remint lands on a pane another active Dispatch already owns', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const staleCtx = d.createDispatchContext(task.id, 'term_a')
    const otherTask = d.createTask({ spec: 'other work' })
    d.createDispatchContext(otherTask.id, 'term_b', 'tab_b:leaf_b')

    const reused = d.findReusableUninjectedDispatchContext(task.id, 'term_a', 'tab_b:leaf_b')

    expect(reused).toBeUndefined()
    expect(d.getDispatchContextById(staleCtx.id)?.status).toBe('dispatched')
  })

  it('refuses reuse when the remint target pane matches by leaf id under a different tab suffix', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    d.createDispatchContext(task.id, 'term_a')
    const otherTask = d.createTask({ spec: 'other work' })
    d.createDispatchContext(
      otherTask.id,
      'term_b',
      'tab_original:11111111-1111-4111-8111-111111111111'
    )

    const reused = d.findReusableUninjectedDispatchContext(
      task.id,
      'term_a',
      'tab_reattached:11111111-1111-4111-8111-111111111111'
    )

    expect(reused).toBeUndefined()
  })

  // Why (#14809): two concurrent --inject retries can both observe the same reused,
  // capability-less context before either mints (the read and the mint straddle an await in
  // the RPC handler). mintDispatchCapability's own conditional UPDATE is the real fence: the
  // second mint on the same dispatch must fail loudly, not silently overwrite the first.
  it('rejects a second mint on the same dispatch once a concurrent request already minted one', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_worker')
    // Both racing callers resolve the same reusable, still-capability-less context.
    expect(d.findReusableUninjectedDispatchContext(task.id, 'term_worker')?.id).toBe(ctx.id)

    d.mintDispatchCapability({
      dispatchId: ctx.id,
      paneKey: 'tab_worker:leaf_worker',
      processIncarnation: 'worker:1'
    })

    expect(() =>
      d.mintDispatchCapability({
        dispatchId: ctx.id,
        paneKey: 'tab_worker:leaf_worker',
        processIncarnation: 'worker:2'
      })
    ).toThrow('already has a lifecycle capability')
  })

  // Why (#14809 round 3): findReusableUninjectedDispatchContext's collision check and
  // mintDispatchCapability's pane-rebind UPDATE straddle an await in the RPC handler. A second,
  // independent dispatch can legitimately claim the target pane in that window (it doesn't collide
  // with the reused row's pane yet, since that row's assignee_pane_key hasn't been rebound). The
  // rebind UPDATE must re-check pane occupancy itself instead of trusting the earlier read, or two
  // active dispatch_contexts rows end up sharing one pane.
  it('refuses to rebind onto a pane a concurrent dispatch claimed after the reuse check ran', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_a')
    // Reuse check runs first and finds no collision, since ctx isn't on tab_x:leaf_x yet.
    expect(d.findReusableUninjectedDispatchContext(task.id, 'term_a', 'tab_x:leaf_x')?.id).toBe(
      ctx.id
    )

    // Concurrent dispatch claims tab_x:leaf_x before the reuse caller's mint runs.
    const otherTask = d.createTask({ spec: 'other work' })
    const other = d.createDispatchContext(otherTask.id, 'term_b', 'tab_x:leaf_x')

    expect(() =>
      d.mintDispatchCapability({
        dispatchId: ctx.id,
        paneKey: 'tab_x:leaf_x',
        processIncarnation: 'worker:1'
      })
    ).toThrow('claimed by another active Dispatch')

    // No partial write: ctx stays un-rebound and un-minted, other keeps sole ownership of the pane.
    const reloaded = d.getDispatchContextById(ctx.id)
    expect(reloaded?.capability_hash).toBeNull()
    expect(reloaded?.assignee_pane_key).not.toBe('tab_x:leaf_x')
    expect(d.getDispatchContextById(other.id)?.assignee_pane_key).toBe('tab_x:leaf_x')
  })

  // Why: same race, but the concurrent claimant lands on the same pane's leaf id under a
  // different tab suffix (a remint) rather than an exact string match.
  it('refuses to rebind when the concurrent claimant matches by leaf id under a different tab suffix', () => {
    const d = createDb()
    const task = d.createTask({ spec: 'work' })
    const ctx = d.createDispatchContext(task.id, 'term_a')
    expect(
      d.findReusableUninjectedDispatchContext(
        task.id,
        'term_a',
        'tab_original:11111111-1111-4111-8111-111111111111'
      )?.id
    ).toBe(ctx.id)

    const otherTask = d.createTask({ spec: 'other work' })
    d.createDispatchContext(
      otherTask.id,
      'term_b',
      'tab_reattached:11111111-1111-4111-8111-111111111111'
    )

    expect(() =>
      d.mintDispatchCapability({
        dispatchId: ctx.id,
        paneKey: 'tab_original:11111111-1111-4111-8111-111111111111',
        processIncarnation: 'worker:1'
      })
    ).toThrow('claimed by another active Dispatch')
  })
})
