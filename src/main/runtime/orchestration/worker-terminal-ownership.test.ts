import { describe, expect, it } from 'vitest'
import { deriveWorkerListLaunchSelection } from './worker-terminal-ownership'

describe('deriveWorkerListLaunchSelection', () => {
  it('returns null agent/model for a null start_options blob (context-only Dispatch)', () => {
    expect(deriveWorkerListLaunchSelection(null)).toEqual({ agent: null, model: null })
  })

  it('reads agent with no model when launch preferences were never set', () => {
    const startOptions = JSON.stringify({ agent: 'codex' })
    expect(deriveWorkerListLaunchSelection(startOptions)).toEqual({
      agent: 'codex',
      model: null
    })
  })

  it('falls back to the requested model while effective is still pending (federated attach)', () => {
    const startOptions = JSON.stringify({
      agent: 'claude',
      launch: {
        requested: { agent: 'claude', model: 'aws-bedrock-opus-5', effort: null },
        effective: null
      }
    })
    expect(deriveWorkerListLaunchSelection(startOptions)).toEqual({
      agent: 'claude',
      model: 'aws-bedrock-opus-5'
    })
  })

  it('prefers the effective model over the requested one once resolved', () => {
    const startOptions = JSON.stringify({
      agent: 'claude',
      launch: {
        requested: { agent: 'claude', model: 'requested-model', effort: null },
        effective: { agent: 'claude', model: 'effective-model', effort: null }
      }
    })
    expect(deriveWorkerListLaunchSelection(startOptions)).toEqual({
      agent: 'claude',
      model: 'effective-model'
    })
  })

  it('returns null agent/model instead of throwing for the column default empty-object blob', () => {
    expect(deriveWorkerListLaunchSelection('{}')).toEqual({ agent: null, model: null })
  })

  it('returns null agent/model instead of throwing for the JSON literal "null" (does not read a null property)', () => {
    // Why: JSON.parse('null') === null, so parsed.launch would throw TypeError without the
    // typeof/null guard — reproduces exactly what JSON.stringify(null) would persist if an
    // untyped startOptions caller ever passed null (dual review, 2026-08-20).
    expect(deriveWorkerListLaunchSelection('null')).toEqual({ agent: null, model: null })
  })

  it('returns null agent/model instead of throwing for malformed JSON', () => {
    expect(deriveWorkerListLaunchSelection('{not valid json')).toEqual({
      agent: null,
      model: null
    })
  })

  it('returns null agent/model for a non-object JSON value (array, number, string)', () => {
    expect(deriveWorkerListLaunchSelection('[]')).toEqual({ agent: null, model: null })
    expect(deriveWorkerListLaunchSelection('5')).toEqual({ agent: null, model: null })
    expect(deriveWorkerListLaunchSelection('"claude"')).toEqual({ agent: null, model: null })
  })

  it('coerces a non-string agent/model to null rather than passing it through', () => {
    const startOptions = JSON.stringify({
      agent: 42,
      launch: { requested: { model: { nested: 'object' } }, effective: null }
    })
    expect(deriveWorkerListLaunchSelection(startOptions)).toEqual({ agent: null, model: null })
  })
})
