// `worker-list --agent` must forward the filter to the RPC call and surface agent/model in text output.
import { beforeEach, describe, expect, it, vi } from 'vitest'

const callMock = vi.fn()

vi.mock('../format', () => ({ printResult: vi.fn() }))
vi.mock('../selectors', () => ({ getTerminalHandle: vi.fn() }))

import { ORCHESTRATION_HANDLERS } from './orchestration'
import { printResult } from '../format'
import { ORCHESTRATION_WORKER_LIST_AGENT_FILTER_RUNTIME_CAPABILITY } from '../../shared/protocol-version'

function renderedText(result: unknown): string {
  const [value, , render] = vi.mocked(printResult).mock.calls[0] as [
    unknown,
    boolean,
    (value: unknown) => string
  ]
  expect(value).toBe(result)
  return render(value)
}

async function listWorkers(
  flags: [string, string | boolean][],
  workers: {
    dispatchId: string
    taskId: string
    workerState: string
    terminalState: string | null
    agent: string | null
    model: string | null
  }[]
): Promise<{ text: string; params: unknown }> {
  const result = { workers, counts: {} }
  // Why: the handler capability-gates via a `status.get` pre-check ONLY when `--agent` is
  // passed (mirrors worker-start's model/effort gate) — mock it first, sequenced, exactly like
  // orchestration-worker-cli.test.ts's worker-start tests, so the pre-check's own
  // `status.result.capabilities` read doesn't land on this call's `{workers, counts}` shape.
  const hasAgentFlag = flags.some(([key]) => key === 'agent')
  if (hasAgentFlag) {
    callMock.mockResolvedValueOnce({
      result: { capabilities: [ORCHESTRATION_WORKER_LIST_AGENT_FILTER_RUNTIME_CAPABILITY] }
    })
  }
  callMock.mockResolvedValueOnce(result)
  await ORCHESTRATION_HANDLERS['orchestration worker-list']({
    flags: new Map<string, string | boolean>(flags),
    client: { call: callMock },
    cwd: '/tmp/repo',
    json: false
  } as never)
  const listCallIndex = hasAgentFlag ? 1 : 0
  return { text: renderedText(result), params: callMock.mock.calls[listCallIndex][1] }
}

describe('orchestration worker-list --agent', () => {
  beforeEach(() => {
    callMock.mockReset()
    vi.mocked(printResult).mockReset()
  })

  it('forwards --agent to the RPC call params', async () => {
    const { params } = await listWorkers(
      [['agent', 'claude']],
      [
        {
          dispatchId: 'd1',
          taskId: 't1',
          workerState: 'ready',
          terminalState: 'active',
          agent: 'claude',
          model: 'aws-bedrock-opus-5'
        }
      ]
    )
    expect(params).toMatchObject({ agent: 'claude' })
  })

  it('shows agent/model in the plain-text row when both are present', async () => {
    const { text } = await listWorkers(
      [],
      [
        {
          dispatchId: 'd1',
          taskId: 't1',
          workerState: 'ready',
          terminalState: 'active',
          agent: 'claude',
          model: 'aws-bedrock-opus-5'
        }
      ]
    )
    expect(text).toContain('agent=claude/aws-bedrock-opus-5')
  })

  it('omits the agent suffix entirely when agent is null', async () => {
    const { text } = await listWorkers(
      [],
      [
        {
          dispatchId: 'd1',
          taskId: 't1',
          workerState: 'unsupervised',
          terminalState: 'retained',
          agent: null,
          model: null
        }
      ]
    )
    expect(text).not.toContain('agent=')
  })

  it('fails before worker-list when the runtime would silently strip --agent (dual review, 2026-08-20)', async () => {
    // Why: WorkerListParams is a plain zod object with no .strict() — an old host drops an
    // unrecognized `agent` field and returns the FULL unfiltered list with no error, which the
    // caller has no way to detect. Mirrors orchestration-worker-cli.test.ts's identical
    // worker-start capability-gate test.
    callMock.mockResolvedValueOnce({ result: { capabilities: [] } })

    await expect(
      ORCHESTRATION_HANDLERS['orchestration worker-list']({
        flags: new Map<string, string | boolean>([['agent', 'claude']]),
        client: { call: callMock },
        cwd: '/tmp/repo',
        json: false
      } as never)
    ).rejects.toMatchObject({ code: 'incompatible_runtime' })

    expect(callMock).toHaveBeenCalledTimes(1)
  })

  it('does not capability-gate when --agent is not passed', async () => {
    await listWorkers(
      [],
      [
        {
          dispatchId: 'd1',
          taskId: 't1',
          workerState: 'ready',
          terminalState: 'active',
          agent: null,
          model: null
        }
      ]
    )
    // Why: no status.get pre-check should fire at all when --agent is absent — only one RPC call.
    expect(callMock).toHaveBeenCalledTimes(1)
  })
})
