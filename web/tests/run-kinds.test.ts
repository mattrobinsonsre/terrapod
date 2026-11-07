import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { availableRunKinds, type RunKindContext } from '../src/lib/run-kinds.ts'

/**
 * Which run kinds a workspace offers, and which is primary (#2413 F12 / F10).
 *
 * The Runs tab offered five controls whose visual weight did not track risk:
 * the two loudest were the safest action and a fleet-changing one, and the two
 * most destructive were the quiet ones. The resolution is one primary action
 * derived from what the workspace declares, so the output of this function IS
 * the fix — the first entry is what gets the loud button.
 *
 * Tested as a cross product of the two workspace flags and the two
 * capabilities, because "which is primary" is a RELATIONSHIP between them: a
 * fixture with one combination passes whichever way the ordering is written.
 */

const ctx = (over: Partial<RunKindContext> = {}): RunKindContext => ({
  planApplyEnabled: true,
  configureEnabled: false,
  canQueueRun: true,
  canQueueApply: true,
  vcsRefSelected: false,
  ...over,
})

const ids = (c: RunKindContext) => availableRunKinds(c).map((k) => k.id)

describe('the primary is the most complete option the workspace declares', () => {
  it('is Full Run when the workspace declares both operations', () => {
    assert.deepEqual(ids(ctx({ configureEnabled: true })), [
      'full',
      'planApply',
      'configure',
      'plan',
    ])
  })

  it('is Plan + apply when only plan/apply is declared', () => {
    // No `full`, because there is no configure for it to include — rather than
    // a "Full Run" that quietly means two phases on one workspace and three on
    // another.
    assert.deepEqual(ids(ctx()), ['planApply', 'plan'])
  })

  it('is Configure when that is the only operation declared', () => {
    // The configure-only workspace #1966 makes possible. `plan` is absent
    // because the workspace declares no plan/apply operation at all, so a plan
    // would be refused by the server.
    assert.deepEqual(ids(ctx({ planApplyEnabled: false, configureEnabled: true })), ['configure'])
  })

  it('offers nothing when the workspace declares neither', () => {
    // Refused at the write boundary, so it should not exist — but if a row ever
    // holds it, offering no run is the honest rendering rather than a button
    // the API refuses.
    assert.deepEqual(ids(ctx({ planApplyEnabled: false, configureEnabled: false })), [])
  })
})

describe('a capability the caller lacks makes a kind ABSENT, not disabled', () => {
  it('leaves a plan-tier caller with Plan alone', () => {
    // #1634: showing an apply button to someone the API refuses is the defect.
    // With one option the caller renders a plain button and no caret, so this
    // is also what removes the menu for a read-mostly role.
    assert.deepEqual(ids(ctx({ configureEnabled: true, canQueueApply: false })), ['plan'])
  })

  it('withholds the configure from a plan-tier caller too', () => {
    // A configure is apply-equivalent — it needs `run:apply` — so the same gate
    // covers it. Stated separately because it is the one that was added last
    // and is easy to key on the wrong capability.
    assert.deepEqual(ids(ctx({ planApplyEnabled: false, configureEnabled: true, canQueueApply: false })), [])
  })

  it('offers the applies but not the plan when only apply is granted', () => {
    // Contrived, and asserted so the two capabilities are shown to be read
    // independently rather than one standing in for both.
    assert.deepEqual(ids(ctx({ configureEnabled: true, canQueueRun: false })), [
      'full',
      'planApply',
      'configure',
    ])
  })
})

describe('a selected VCS ref blocks the applies and not the configure', () => {
  it('marks the apply-carrying kinds blocked, and keeps them present', () => {
    const got = availableRunKinds(ctx({ configureEnabled: true, vcsRefSelected: true }))
    assert.deepEqual(
      got.map((k) => [k.id, k.blocked ?? null]),
      [
        ['full', 'vcsRef'],
        ['planApply', 'vcsRef'],
        // The configure is NOT blocked: the ref forcing applies to the
        // plan/apply half, and a standalone configure has no plan to force.
        ['configure', null],
        ['plan', null],
      ],
    )
  })

  it('present and blocked rather than absent, which is the F20 split', () => {
    // The kind IS declared and the caller MAY run it — it just cannot right
    // now — so hiding it would remove the explanation along with the control.
    const got = availableRunKinds(ctx({ vcsRefSelected: true }))
    assert.ok(
      got.some((k) => k.id === 'planApply' && k.blocked === 'vcsRef'),
      'plan + apply should be offered and explained, not hidden',
    )
  })

  it('leaves a plan runnable, because that is what the ref forces it to be', () => {
    const got = availableRunKinds(ctx({ vcsRefSelected: true }))
    assert.equal(got.find((k) => k.id === 'plan')?.blocked, undefined)
  })
})

describe('destroy is not a run kind here', () => {
  it('never appears, under any combination', () => {
    // Its own grant (`run:apply-destroy`) and its own inline two-step. Burying
    // the most consequential action in a menu is the one place a caret costs
    // more than it saves, so it stays a button of its own.
    for (const planApplyEnabled of [true, false]) {
      for (const configureEnabled of [true, false]) {
        for (const canQueueApply of [true, false]) {
          const got = ids(ctx({ planApplyEnabled, configureEnabled, canQueueApply }))
          assert.ok(!got.includes('destroy' as never), `destroy leaked: ${got.join(',')}`)
        }
      }
    }
  })
})
