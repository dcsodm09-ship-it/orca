import { describe, expect, it, vi } from 'vitest'

const callMock = vi.fn()

// Why: isolate the handler's flag-to-param mapping; printResult only writes output.
vi.mock('../format', () => ({ printResult: vi.fn() }))

import { ORCHESTRATION_HANDLERS } from './orchestration'

// Why (#14809): an un-injected dispatch still commits and claims the terminal's one
// active-dispatch slot, so stdout alone can miss that nothing reached the agent.
describe('orchestration dispatch injected:false warning', () => {
  const invokeDispatch = (flags: Map<string, string | boolean>, json: boolean) =>
    ORCHESTRATION_HANDLERS['orchestration dispatch']({
      flags,
      client: { call: callMock },
      cwd: '/tmp/repo',
      json
    } as never)

  it('warns on stderr when a dispatch commits without being injected', async () => {
    callMock.mockReset().mockResolvedValueOnce({
      result: {
        dispatch: { id: 'ctx_1', task_id: 'task_1', status: 'dispatched' },
        injected: false
      }
    })
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    await invokeDispatch(
      new Map<string, string | boolean>([
        ['task', 'task_1'],
        ['to', 'term_worker'],
        ['from', 'term_coord']
      ]),
      false
    )

    expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining('not injected'))
    errorSpy.mockRestore()
  })

  it('does not warn when the dispatch was actually injected', async () => {
    callMock.mockReset().mockResolvedValueOnce({
      result: {
        dispatch: { id: 'ctx_1', task_id: 'task_1', status: 'dispatched' },
        injected: true
      }
    })
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    await invokeDispatch(
      new Map<string, string | boolean>([
        ['task', 'task_1'],
        ['to', 'term_worker'],
        ['from', 'term_coord'],
        ['inject', true]
      ]),
      false
    )

    expect(errorSpy).not.toHaveBeenCalled()
    errorSpy.mockRestore()
  })

  it('does not warn under --json even when a dispatch was not injected', async () => {
    callMock.mockReset().mockResolvedValueOnce({
      result: {
        dispatch: { id: 'ctx_1', task_id: 'task_1', status: 'dispatched' },
        injected: false
      }
    })
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    await invokeDispatch(
      new Map<string, string | boolean>([
        ['task', 'task_1'],
        ['to', 'term_worker'],
        ['from', 'term_coord']
      ]),
      true
    )
    expect(callMock).toHaveBeenCalledTimes(1)

    expect(errorSpy).not.toHaveBeenCalled()
    errorSpy.mockRestore()
  })
})
