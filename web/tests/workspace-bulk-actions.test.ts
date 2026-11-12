/**
 * Which bulk actions a selection of workspaces can take.
 *
 * Each action acts on a workspace's LATEST run (the list already carries it),
 * except Plan, which queues a new run. The rule is "every selected row
 * qualifies or the action is off": a partial batch is hard to reason about, and
 * the dropdown says how many qualify (`Apply · 9 of 12`) rather than hiding the
 * reason in a tooltip.
 *
 * The status sets mirror the server: retry needs a terminal run, cancel needs
 * an in-progress one (`CANCELABLE_STATES`), and apply and discard need a
 * `planned` run that is not plan-only.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  BULK_ACTIONS,
  eligibleFor,
  summarise,
  type BulkWorkspace,
} from '../src/lib/workspace-bulk-actions.ts'

function ws(id: string, run: { status: string; planOnly?: boolean } | null, locked = false): BulkWorkspace {
  return {
    id,
    name: `ws-${id}`,
    locked,
    latestRun: run && { id: `run-${id}`, status: run.status, planOnly: run.planOnly ?? false },
  }
}

const ids = (list: BulkWorkspace[]) => list.map((w) => w.id)

describe('retry: the latest run is terminal', () => {
  it('accepts applied, errored, discarded and canceled', () => {
    for (const status of ['applied', 'errored', 'discarded', 'canceled']) {
      assert.deepEqual(ids(eligibleFor('retry', [ws('a', { status })])), ['a'], status)
    }
  })

  it('accepts a plan-only run that has planned, which the server treats as finished', () => {
    assert.deepEqual(ids(eligibleFor('retry', [ws('a', { status: 'planned', planOnly: true })])), ['a'])
  })

  it('refuses an apply-capable planned run, a running run and no run at all', () => {
    assert.deepEqual(eligibleFor('retry', [ws('a', { status: 'planned' })]), [])
    assert.deepEqual(eligibleFor('retry', [ws('a', { status: 'planning' })]), [])
    assert.deepEqual(eligibleFor('retry', [ws('a', null)]), [])
  })
})

describe('cancel: the latest run is in progress', () => {
  it('accepts every cancelable state', () => {
    for (const status of ['pending', 'queued', 'planning', 'confirmed', 'applying', 'configuring']) {
      assert.deepEqual(ids(eligibleFor('cancel', [ws('a', { status })])), ['a'], status)
    }
  })

  it('refuses planned: a finished plan is resolved by apply or discard, not cancel', () => {
    assert.deepEqual(eligibleFor('cancel', [ws('a', { status: 'planned' })]), [])
  })

  it('refuses terminal runs and no run', () => {
    assert.deepEqual(eligibleFor('cancel', [ws('a', { status: 'applied' }), ws('b', null)]), [])
  })
})

describe('apply and discard: a planned run that can be applied', () => {
  it('accept a planned, apply-capable run', () => {
    assert.deepEqual(ids(eligibleFor('apply', [ws('a', { status: 'planned' })])), ['a'])
    assert.deepEqual(ids(eligibleFor('discard', [ws('a', { status: 'planned' })])), ['a'])
  })

  it('refuse a plan-only plan: there is nothing to apply or discard', () => {
    assert.deepEqual(eligibleFor('apply', [ws('a', { status: 'planned', planOnly: true })]), [])
    assert.deepEqual(eligibleFor('discard', [ws('a', { status: 'planned', planOnly: true })]), [])
  })

  it('refuse any other status', () => {
    for (const status of ['planning', 'applying', 'applied', 'errored', 'queued']) {
      assert.deepEqual(eligibleFor('apply', [ws('a', { status })]), [], status)
      assert.deepEqual(eligibleFor('discard', [ws('a', { status })]), [], status)
    }
  })

  it('refuses apply on a locked workspace, since the server refuses it, but not discard', () => {
    assert.deepEqual(eligibleFor('apply', [ws('a', { status: 'planned' }, true)]), [])
    assert.deepEqual(ids(eligibleFor('discard', [ws('a', { status: 'planned' }, true)])), ['a'])
  })
})

describe('plan: queues a new run, so it needs no existing run', () => {
  it('accepts a workspace with no run, and one with any run', () => {
    assert.deepEqual(
      ids(eligibleFor('plan', [ws('a', null), ws('b', { status: 'applied' }), ws('c', { status: 'planning' })])),
      ['a', 'b', 'c'],
    )
  })

  it('accepts a locked workspace: the server queues a plan on one (verified against the real API)', () => {
    // A manual lock blocks an APPLY, not a plan. This rule used to refuse a locked
    // workspace, a guess that turned out to be wrong when it was run against the
    // real server, which answers 201 and queues the plan.
    assert.deepEqual(ids(eligibleFor('plan', [ws('a', null, true)])), ['a'])
  })
})

describe('summarise: the row text and whether the action is enabled', () => {
  it('is enabled only when every selected row qualifies', () => {
    const all = [ws('a', { status: 'planned' }), ws('b', { status: 'planned' })]
    assert.deepEqual(summarise('apply', all), { eligible: 2, total: 2, enabled: true })
  })

  it('counts the eligible rows when only some qualify, and is off', () => {
    const mixed = [ws('a', { status: 'planned' }), ws('b', { status: 'applied' }), ws('c', { status: 'planned' })]
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

  it('marks only apply and discard as needing a confirmation', () => {
    assert.deepEqual(
      BULK_ACTIONS.filter((a) => a.confirm).map((a) => a.id),
      ['apply', 'discard'],
    )
  })
})
