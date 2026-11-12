/**
 * Running a bulk action calls the EXISTING per-run endpoints once per selected
 * workspace and reports what happened to each. Nothing here is new API surface:
 * the server stays authoritative, so a workspace the caller may not act on, or
 * whose run has moved on, simply comes back refused with the server's reason.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { runBulkAction, endpointFor, type Fetcher } from '../src/lib/workspace-bulk-run.ts'
import type { BulkWorkspace } from '../src/lib/workspace-bulk-actions.ts'

function ws(id: string, status: string | null, planOnly = false): BulkWorkspace {
  return {
    id: `ws-${id}`,
    name: `name-${id}`,
    locked: false,
    latestRun: status ? { id: `run-${id}`, status, planOnly } : null,
  }
}

interface Call {
  path: string
  method: string
  body: unknown
}

// A fetcher that records each call and answers by workspace, as the server would.
function recorder(answer: (path: string) => { ok: boolean; status: number; detail?: string }) {
  const calls: Call[] = []
  const fetcher: Fetcher = async (path, init) => {
    calls.push({ path, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const a = answer(path)
    return {
      ok: a.ok,
      status: a.status,
      json: async () => (a.detail ? { errors: [{ detail: a.detail }] } : {}),
    } as unknown as Response
  }
  return { calls, fetcher }
}

describe('endpointFor', () => {
  it('maps the run actions onto the existing run endpoints', () => {
    const w = ws('a', 'planned')
    assert.equal(endpointFor('apply', w)?.path, '/api/v1/runs/run-a/actions/apply')
    assert.equal(endpointFor('discard', w)?.path, '/api/v1/runs/run-a/actions/discard')
    assert.equal(endpointFor('cancel', w)?.path, '/api/v1/runs/run-a/actions/cancel')
    assert.equal(endpointFor('retry', w)?.path, '/api/v1/runs/run-a/actions/retry')
  })

  it('plan creates a plan-only run on the workspace, not an action on a run', () => {
    const e = endpointFor('plan', ws('a', null))
    assert.equal(e?.path, '/api/v1/runs')
    assert.deepEqual(e?.body, {
      data: {
        type: 'runs',
        attributes: { 'plan-only': true },
        relationships: { workspace: { data: { type: 'workspaces', id: 'ws-a' } } },
      },
    })
  })

  it('has no endpoint for an action that needs a run the workspace does not have', () => {
    assert.equal(endpointFor('apply', ws('a', null)), null)
  })
})

describe('runBulkAction', () => {
  it('calls the endpoint once per workspace, with POST', async () => {
    const { calls, fetcher } = recorder(() => ({ ok: true, status: 200 }))
    await runBulkAction('apply', [ws('a', 'planned'), ws('b', 'planned')], fetcher)
    assert.deepEqual(
      calls.map((c) => [c.method, c.path]),
      [
        ['POST', '/api/v1/runs/run-a/actions/apply'],
        ['POST', '/api/v1/runs/run-b/actions/apply'],
      ],
    )
  })

  it('reports each workspace separately, with the server reason on a refusal', async () => {
    const { fetcher } = recorder((path) =>
      path.includes('run-b')
        ? { ok: false, status: 409, detail: 'workspace is locked — unlock before applying' }
        : { ok: true, status: 200 },
    )
    const results = await runBulkAction('apply', [ws('a', 'planned'), ws('b', 'planned')], fetcher)
    assert.deepEqual(results, [
      { id: 'ws-a', name: 'name-a', ok: true },
      { id: 'ws-b', name: 'name-b', ok: false, reason: 'workspace is locked — unlock before applying' },
    ])
  })

  it('keeps going after a refusal, so one workspace does not stop the rest', async () => {
    const { calls, fetcher } = recorder((path) =>
      path.includes('run-a') ? { ok: false, status: 403, detail: 'forbidden' } : { ok: true, status: 200 },
    )
    const results = await runBulkAction('cancel', [ws('a', 'planning'), ws('b', 'planning'), ws('c', 'planning')], fetcher)
    assert.equal(calls.length, 3)
    assert.deepEqual(results.map((r) => r.ok), [false, true, true])
  })

  it('reports a network failure as a refusal for that workspace, not a crash', async () => {
    const fetcher: Fetcher = async () => {
      throw new TypeError('network down')
    }
    const results = await runBulkAction('retry', [ws('a', 'errored')], fetcher)
    assert.equal(results[0].ok, false)
    assert.match(String((results[0] as { reason: string }).reason), /network down/)
  })

  it('does not send the request for a workspace that no longer has the run, and says so', async () => {
    const { calls, fetcher } = recorder(() => ({ ok: true, status: 200 }))
    const results = await runBulkAction('apply', [ws('a', null)], fetcher)
    assert.equal(calls.length, 0)
    assert.equal(results[0].ok, false)
  })

  it('returns nothing for an empty selection', async () => {
    const { calls, fetcher } = recorder(() => ({ ok: true, status: 200 }))
    assert.deepEqual(await runBulkAction('apply', [], fetcher), [])
    assert.equal(calls.length, 0)
  })
})
