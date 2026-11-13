// The server's answer decides eligibility; the lib adds only the lock and the declared operations.
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  BULK_ACTIONS,
  eligibleFor,
  summarise,
  type BulkRunActions,
  type BulkWorkspace,
} from '../src/lib/workspace-bulk-actions.ts'

const NONE: BulkRunActions = { confirmable: false, discardable: false, cancelable: false, retryable: false }

interface Opts {
  actions?: Partial<BulkRunActions> | null
  runKind?: string | null
  locked?: boolean
  planApply?: boolean
  configure?: boolean
  noRun?: boolean
}

function ws(id: string, o: Opts = {}): BulkWorkspace {
  return {
    id,
    name: `ws-${id}`,
    locked: o.locked ?? false,
    planApplyEnabled: o.planApply ?? true,
    configureEnabled: o.configure ?? false,
    latestRun: o.noRun
      ? null
      : {
          id: `run-${id}`,
          runKind: o.runKind === undefined ? 'plan_apply' : o.runKind,
          actions: o.actions === null ? null : { ...NONE, ...o.actions },
        },
  }
}

const ids = (list: BulkWorkspace[]) => list.map((w) => w.id)

describe('retry: the server says the latest run is retryable', () => {
  it('follows actions.retryable', () => {
    assert.deepEqual(ids(eligibleFor('retry', [ws('a', { actions: { retryable: true } })])), ['a'])
    assert.deepEqual(eligibleFor('retry', [ws('a', { actions: { retryable: false } })]), [])
  })

  it('refuses no run at all', () => {
    assert.deepEqual(eligibleFor('retry', [ws('a', { noRun: true })]), [])
  })

  it('needs plan-apply for a plan/apply retry', () => {
    const a = { retryable: true }
    assert.deepEqual(eligibleFor('retry', [ws('a', { actions: a, planApply: false, configure: true })]), [])
    assert.deepEqual(ids(eligibleFor('retry', [ws('a', { actions: a, planApply: true })])), ['a'])
  })

  it('retries a configure on a configure-only workspace, unless it is locked', () => {
    const o = { actions: { retryable: true }, runKind: 'configure', planApply: false, configure: true }
    assert.deepEqual(ids(eligibleFor('retry', [ws('a', o)])), ['a'])
    assert.deepEqual(eligibleFor('retry', [ws('a', { ...o, locked: true })]), [])
  })

  it('does not retry a plan/apply run on a configure-only workspace', () => {
    const o = { actions: { retryable: true }, runKind: 'plan_apply', planApply: false, configure: true }
    assert.deepEqual(eligibleFor('retry', [ws('a', o)]), [])
  })

  it('retries a combined run only with both operations enabled and an unlocked workspace', () => {
    const o = { actions: { retryable: true }, runKind: 'full', planApply: true, configure: true }
    assert.deepEqual(ids(eligibleFor('retry', [ws('a', o)])), ['a'])
    assert.deepEqual(eligibleFor('retry', [ws('a', { ...o, locked: true })]), [])
    assert.deepEqual(eligibleFor('retry', [ws('a', { ...o, planApply: false })]), [])
    assert.deepEqual(eligibleFor('retry', [ws('a', { ...o, configure: false })]), [])
  })

  it('does not retry a configure when configure is not enabled', () => {
    const o = { actions: { retryable: true }, runKind: 'configure', configure: false }
    assert.deepEqual(eligibleFor('retry', [ws('a', o)]), [])
  })
})

describe('cancel: the server says the latest run is cancelable', () => {
  it('follows actions.cancelable', () => {
    assert.deepEqual(ids(eligibleFor('cancel', [ws('a', { actions: { cancelable: true } })])), ['a'])
    assert.deepEqual(eligibleFor('cancel', [ws('a', { actions: { cancelable: false } }), ws('b', { noRun: true })]), [])
  })
})

describe('apply: the server says confirmable, and the workspace is not locked', () => {
  it('follows actions.confirmable, whatever the status', () => {
    assert.deepEqual(ids(eligibleFor('apply', [ws('a', { actions: { confirmable: true } })])), ['a'])
    assert.deepEqual(eligibleFor('apply', [ws('a', { actions: { confirmable: false, discardable: true } })]), [])
  })

  it('refuses a locked workspace, since the server refuses it', () => {
    assert.deepEqual(eligibleFor('apply', [ws('a', { actions: { confirmable: true }, locked: true })]), [])
  })
})

describe('discard: the server says discardable', () => {
  it('follows actions.discardable, including a run held after its plan', () => {
    assert.deepEqual(ids(eligibleFor('discard', [ws('a', { actions: { discardable: true } })])), ['a'])
    assert.deepEqual(eligibleFor('discard', [ws('a', { actions: { discardable: false } })]), [])
  })

  it('is not blocked by a lock', () => {
    assert.deepEqual(ids(eligibleFor('discard', [ws('a', { actions: { discardable: true }, locked: true })])), ['a'])
  })
})

describe('no actions from the server (an older API): fail closed', () => {
  it('offers only Plan', () => {
    const w = ws('a', { actions: null })
    for (const action of ['retry', 'cancel', 'apply', 'discard'] as const) {
      assert.deepEqual(eligibleFor(action, [w]), [], action)
    }
    assert.deepEqual(ids(eligibleFor('plan', [w])), ['a'])
    assert.deepEqual(eligibleFor('retry', [ws('b', { actions: null, runKind: null })]), [])
  })
})

describe('plan: queues a new run, so it needs no existing run', () => {
  it('accepts a workspace with no run, and one with any run', () => {
    assert.deepEqual(ids(eligibleFor('plan', [ws('a', { noRun: true }), ws('b'), ws('c', { actions: null })])), ['a', 'b', 'c'])
  })

  it('accepts a locked workspace: a lock blocks an apply, not a plan', () => {
    assert.deepEqual(ids(eligibleFor('plan', [ws('a', { noRun: true, locked: true })])), ['a'])
  })

  it('refuses a workspace without plan/apply enabled', () => {
    assert.deepEqual(eligibleFor('plan', [ws('a', { planApply: false, configure: true })]), [])
  })
})

describe('summarise: the row text and whether the action is enabled', () => {
  it('is enabled only when every selected row qualifies', () => {
    const all = [ws('a', { actions: { confirmable: true } }), ws('b', { actions: { confirmable: true } })]
    assert.deepEqual(summarise('apply', all), { eligible: 2, total: 2, enabled: true })
  })

  it('counts the eligible rows when only some qualify, and is off', () => {
    const mixed = [ws('a', { actions: { confirmable: true } }), ws('b'), ws('c', { actions: { confirmable: true } })]
    assert.deepEqual(summarise('apply', mixed), { eligible: 2, total: 3, enabled: false })
  })

  it('is off for an empty selection, not vacuously on', () => {
    assert.deepEqual(summarise('apply', []), { eligible: 0, total: 0, enabled: false })
  })
})

describe('the action list', () => {
  it('offers exactly Plan, Retry, Cancel, Apply and Discard, in that order', () => {
    assert.deepEqual(
      BULK_ACTIONS.map((a) => a.id),
      ['plan', 'retry', 'cancel', 'apply', 'discard'],
    )
  })

  it('puts Apply, Discard and Retry in the irreversible tier, Plan and Cancel in the touch tier', () => {
    assert.deepEqual(
      BULK_ACTIONS.map((a) => [a.id, a.confirm]),
      [
        ['plan', 'touch'],
        ['retry', 'irreversible'],
        ['cancel', 'touch'],
        ['apply', 'irreversible'],
        ['discard', 'irreversible'],
      ],
    )
  })
})
