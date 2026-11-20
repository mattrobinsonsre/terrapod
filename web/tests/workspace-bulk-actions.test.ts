// The server's answer decides eligibility; the lib adds only the lock and the declared operations.
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  BULK_ACTIONS,
  confirmTier,
  eligibleFor,
  summarise,
  type BulkPermissions,
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
  planOnly?: boolean
  isDestroy?: boolean | null
  /** Defaults to all three GRANTED, so a test that says nothing about
   *  permission goes on testing run-state eligibility and nothing else. */
  perms?: Partial<BulkPermissions>
}

function ws(id: string, o: Opts = {}): BulkWorkspace {
  return {
    id,
    name: `ws-${id}`,
    locked: o.locked ?? false,
    planApplyEnabled: o.planApply ?? true,
    configureEnabled: o.configure ?? false,
    permissions: { canQueueRun: true, canQueueApply: true, canQueueDestroy: true, ...o.perms },
    latestRun: o.noRun
      ? null
      : {
          id: `run-${id}`,
          runKind: o.runKind === undefined ? 'plan_apply' : o.runKind,
          planOnly: o.planOnly,
          isDestroy: o.isDestroy === null ? undefined : (o.isDestroy ?? false),
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

describe('which pointer a confirmation is for', () => {
  const tier = (action: Parameters<typeof confirmTier>[0], ...sel: BulkWorkspace[]) => confirmTier(action, sel)

  it('Apply and Discard always confirm; Plan and Cancel confirm on touch only', () => {
    const run = ws('a', { planOnly: true })
    assert.equal(tier('apply', run), 'irreversible')
    assert.equal(tier('discard', run), 'irreversible')
    assert.equal(tier('plan', run), 'touch')
    assert.equal(tier('cancel', run), 'touch')
  })

  it('a retry of a plan-only run applies nothing, so it confirms on touch only', () => {
    assert.equal(tier('retry', ws('a', { planOnly: true })), 'touch')
  })

  it('a retry that can apply confirms on every pointer', () => {
    assert.equal(tier('retry', ws('a', { planOnly: false })), 'irreversible')
  })

  it('a configure or combined run is never read-only, even when it says plan-only', () => {
    assert.equal(tier('retry', ws('a', { planOnly: true, runKind: 'configure' })), 'irreversible')
    assert.equal(tier('retry', ws('a', { planOnly: true, runKind: 'full' })), 'irreversible')
  })

  it('a destroy confirms on every pointer, plan-only or not', () => {
    assert.equal(tier('retry', ws('a', { planOnly: true, isDestroy: true })), 'irreversible')
    assert.equal(tier('retry', ws('a', { planOnly: false, isDestroy: true })), 'irreversible')
    assert.equal(tier('retry', ws('a', { planOnly: true }), ws('b', { planOnly: true, isDestroy: true })), 'irreversible')
  })

  it('fails toward confirming when the kind or plan-only is not known', () => {
    assert.equal(tier('retry', ws('a', { planOnly: true, isDestroy: null })), 'irreversible')
    assert.equal(tier('retry', ws('a')), 'irreversible')
    assert.equal(tier('retry', ws('a', { planOnly: true, runKind: null })), 'irreversible')
    assert.equal(tier('retry', ws('a', { planOnly: true, runKind: 'something-new' })), 'irreversible')
    assert.equal(tier('retry', ws('a', { noRun: true })), 'irreversible')
    assert.equal(tier('retry'), 'irreversible')
  })

  it('one apply-capable run in the selection makes the whole retry confirm on every pointer', () => {
    const readOnly = ws('a', { planOnly: true })
    assert.equal(tier('retry', readOnly, ws('b', { planOnly: true })), 'touch')
    assert.equal(tier('retry', readOnly, ws('b', { planOnly: false })), 'irreversible')
    assert.equal(tier('retry', readOnly, ws('b')), 'irreversible')
  })
})

describe('the confirmation text', () => {
  const page = readFileSync(new URL('../src/app/workspaces/page.tsx', import.meta.url), 'utf8')
  const en = JSON.parse(readFileSync(new URL('../messages/en.json', import.meta.url), 'utf8'))

  // A literal `t('bulk.confirmX')` is what the key-resolve gate can see; a built key is skipped.
  it('has a literal key in the page and a string in en.json for every action', () => {
    for (const { id } of BULK_ACTIONS) {
      const key = `bulk.confirm${id[0].toUpperCase()}${id.slice(1)}`
      assert.ok(page.includes(`t('${key}'`), `${key} is not requested as a literal`)
      assert.equal(typeof en.workspaces.bulk[key.slice('bulk.'.length)], 'string', `${key} missing from en.json`)
    }
  })

  it('does not build a key at runtime', () => {
    assert.ok(!page.includes('`bulk.confirm'))
  })
})

describe('permission gating (#2579 item 2)', () => {
  // The menu read only run STATE until now, so a read-only user was offered
  // Apply on any `planned` run and got a 403 on press. These assert the other
  // dimension: whether this user MAY act.
  const ALL: BulkRunActions = {
    confirmable: true,
    discardable: true,
    cancelable: true,
    retryable: true,
  }

  it('offers nothing when no permission is published at all', () => {
    // Fail closed. An older API without the flags, or a role with none of
    // them, must not be offered a control the server will refuse.
    const w = ws('a', { actions: ALL, perms: { canQueueRun: false, canQueueApply: false, canQueueDestroy: false } })
    for (const { id } of BULK_ACTIONS) {
      assert.deepEqual(ids(eligibleFor(id, [w])), [], `${id} was offered with no permission`)
    }
  })

  it('gates plan on can-queue-run', () => {
    assert.deepEqual(ids(eligibleFor('plan', [ws('a', { perms: { canQueueRun: false } })])), [])
    assert.deepEqual(ids(eligibleFor('plan', [ws('a', { perms: { canQueueRun: true } })])), ['a'])
  })

  it('gates cancel and discard on can-queue-run, since both need run:cancel', () => {
    // run:cancel has no published flag and sits in the same `plan` tier as
    // run:plan, so can-queue-run answers for it (Matt's call, #2621).
    for (const action of ['cancel', 'discard'] as const) {
      const denied = ws('a', { actions: ALL, perms: { canQueueRun: false } })
      const allowed = ws('a', { actions: ALL, perms: { canQueueRun: true } })
      assert.deepEqual(ids(eligibleFor(action, [denied])), [], `${action} offered without run:cancel`)
      assert.deepEqual(ids(eligibleFor(action, [allowed])), ['a'])
    }
  })

  it('gates apply on can-queue-apply, and on can-queue-destroy for a destroy run', () => {
    // confirm_capability(is_destroy) -> RUN_APPLY_DESTROY if destroy else RUN_APPLY.
    const normal = { actions: ALL, isDestroy: false } as const
    const destroy = { actions: ALL, isDestroy: true } as const

    assert.deepEqual(ids(eligibleFor('apply', [ws('a', { ...normal, perms: { canQueueApply: true } })])), ['a'])
    assert.deepEqual(ids(eligibleFor('apply', [ws('a', { ...normal, perms: { canQueueApply: false } })])), [])

    // The distinguishing pair: apply-but-not-destroy must NOT reach a destroy run.
    assert.deepEqual(
      ids(eligibleFor('apply', [ws('a', { ...destroy, perms: { canQueueApply: true, canQueueDestroy: false } })])),
      [],
      'a destroy run was offered to a role holding run:apply but not run:apply-destroy',
    )
    assert.deepEqual(
      ids(eligibleFor('apply', [ws('a', { ...destroy, perms: { canQueueApply: false, canQueueDestroy: true } })])),
      ['a'],
    )
  })

  it('gates retry on what CREATING that run would require', () => {
    // create_run_capability(plan_only, is_destroy, is_configure): a retry is a
    // new run, so it must not let anyone queue what they could not queue
    // directly (#1599).
    const base = { actions: ALL } as const

    // plan-only -> run:plan
    assert.deepEqual(
      ids(eligibleFor('retry', [ws('a', { ...base, planOnly: true, perms: { canQueueRun: true, canQueueApply: false } })])),
      ['a'],
    )
    // apply-capable -> run:apply, NOT run:plan
    assert.deepEqual(
      ids(eligibleFor('retry', [ws('a', { ...base, planOnly: false, perms: { canQueueRun: true, canQueueApply: false } })])),
      [],
      'an apply-capable run was retryable by a plan-only role — the #1599 escape',
    )
    // destroy -> run:apply-destroy
    assert.deepEqual(
      ids(eligibleFor('retry', [
        ws('a', { ...base, planOnly: false, isDestroy: true, perms: { canQueueApply: true, canQueueDestroy: false } }),
      ])),
      [],
    )
    // a configure is apply-equivalent and has no destroy analogue
    assert.deepEqual(
      ids(eligibleFor('retry', [
        ws('a', { ...base, runKind: 'configure', configure: true, perms: { canQueueApply: true, canQueueRun: false } }),
      ])),
      ['a'],
    )
    assert.deepEqual(
      ids(eligibleFor('retry', [
        ws('a', { ...base, runKind: 'configure', configure: true, perms: { canQueueApply: false, canQueueRun: true } }),
      ])),
      [],
    )
  })

  it('needs BOTH permission and run state, which neither alone establishes', () => {
    // The property is a relationship, so a fixture holding one dimension
    // constant passes whichever way the code is written.
    const permitted_ineligible = ws('a', { actions: null, perms: { canQueueApply: true } })
    const eligible_unpermitted = ws('b', { actions: ALL, perms: { canQueueApply: false } })
    const both = ws('c', { actions: ALL, perms: { canQueueApply: true } })
    assert.deepEqual(ids(eligibleFor('apply', [permitted_ineligible, eligible_unpermitted, both])), ['c'])
  })

  it('summarise counts an unpermitted row as not eligible, so the menu disables', () => {
    const sel = [ws('a', { actions: ALL, perms: { canQueueApply: true } }), ws('b', { actions: ALL, perms: { canQueueApply: false } })]
    assert.deepEqual(summarise('apply', sel), { eligible: 1, total: 2, enabled: false })
  })
})
