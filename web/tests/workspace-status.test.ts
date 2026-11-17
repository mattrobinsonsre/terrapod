/**
 * `runStatusOf` is what `status:` filters on: the latest run's own status,
 * not the badge's resolved one, which lets a workspace-level condition win.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { RUN_STATUSES, resolveStatus, runStatusCounts, runStatusOf } from '../src/lib/workspace-status.ts'
import { matchWorkspace, parseFilterQuery } from '../src/lib/workspace-filter.ts'

function wsWith(
  run: { status: string; 'plan-only'?: boolean } | null,
  extra: { diverged?: boolean; vcsError?: string | null; drift?: string } = {},
) {
  const conditions = [
    extra.diverged ? 'state_diverged' : null,
    extra.vcsError ? 'vcs_error' : null,
    extra.drift === 'drifted' ? 'drifted' : null,
  ].filter((code) => code !== null)
  return {
    attributes: {
      name: 'a',
      'health-conditions': conditions.map((code) => ({ code })),
      'state-diverged': extra.diverged ?? false,
      'vcs-last-error': extra.vcsError ?? null,
      'drift-status': extra.drift ?? '',
      'latest-run': run ? { id: 'run-1', 'plan-only': false, ...run } : null,
    },
  }
}

const matches = (q: string, w: ReturnType<typeof wsWith>) => matchWorkspace(w, parseFilterQuery(q))

describe('runStatusOf', () => {
  it('is the latest run status for every run status the dropdown offers', () => {
    for (const s of RUN_STATUSES) {
      if (s.filter === 'needs-confirm') continue
      assert.equal(runStatusOf(wsWith({ status: s.filter, 'plan-only': true })), s.filter, s.filter)
    }
  })

  it('reads a planned run that awaits confirmation as needs-confirm, a plan-only one as planned', () => {
    assert.equal(runStatusOf(wsWith({ status: 'planned' })), 'needs-confirm')
    assert.equal(runStatusOf(wsWith({ status: 'planned', 'plan-only': true })), 'planned')
  })

  it('is undefined with no latest run, or a status the dropdown does not offer', () => {
    assert.equal(runStatusOf(wsWith(null)), undefined)
    assert.equal(runStatusOf(wsWith({ status: 'no-such-status' })), undefined)
  })

  it('ignores workspace-level conditions that resolveStatus ranks above the run', () => {
    const w = wsWith({ status: 'errored' }, { diverged: true, vcsError: 'x', drift: 'drifted' })
    assert.equal(resolveStatus(w).def?.filter, 'state-diverged')
    assert.equal(runStatusOf(w), 'errored')
  })
})

describe('status: against the run, not the badge', () => {
  // Each workspace here shows a pill that is NOT its run's status, so a matcher
  // fed the pill (`resolveStatus`) answers differently and these fail.
  it('status:errored finds an errored run on a drifted, diverged or VCS-error workspace', () => {
    for (const extra of [{ drift: 'drifted' }, { diverged: true }, { vcsError: 'boom' }]) {
      const w = wsWith({ status: 'errored' }, extra)
      assert.notEqual(resolveStatus(w).def?.filter, 'errored', JSON.stringify(extra))
      assert.equal(matches('status:errored', w), true, JSON.stringify(extra))
      assert.equal(matches('status:applied', w), false, JSON.stringify(extra))
    }
  })

  it('the pill\'s own value is not a status: the condition is flag:\'s', () => {
    const w = wsWith({ status: 'errored' }, { drift: 'drifted', diverged: true })
    assert.equal(resolveStatus(w).def?.filter, 'state-diverged')
    assert.equal(matches('status:state-diverged', w), false)
    assert.equal(matches('status:drifted', w), false)
    assert.equal(matches('flag:drifted flag:state-diverged', w), true)
  })

  it('a plan-only planned run on a drifted workspace is planned, not needs-confirm', () => {
    const w = wsWith({ status: 'planned', 'plan-only': true }, { drift: 'drifted' })
    assert.equal(resolveStatus(w).def?.filter, 'drifted')
    assert.equal(matches('status:planned', w), true)
    assert.equal(matches('status:needs-confirm', w), false)
  })

  it('status:needs-confirm matches only a planned run that is not plan-only', () => {
    const awaiting = wsWith({ status: 'planned' }, { diverged: true })
    assert.equal(resolveStatus(awaiting).def?.filter, 'needs-confirm')
    assert.equal(matches('status:needs-confirm', awaiting), true)
    assert.equal(matches('status:planned', awaiting), false)
    assert.equal(matches('status:needs-confirm', wsWith({ status: 'planned', 'plan-only': true })), false)
    assert.equal(matches('status:needs-confirm', wsWith({ status: 'applied' })), false)
  })

  it('a workspace with no latest run matches no status: value, whatever its pill shows', () => {
    const w = wsWith(null, { drift: 'drifted', diverged: true })
    assert.equal(resolveStatus(w).def?.filter, 'state-diverged')
    for (const s of RUN_STATUSES) assert.equal(matches(`status:${s.filter}`, w), false, s.filter)
    assert.equal(matches('status:errored status:applied', w), false)
  })
})

describe('runStatusCounts', () => {
  it('counts the run statuses the matcher matches, not the pills', () => {
    const list = [
      wsWith({ status: 'errored' }, { drift: 'drifted' }),
      wsWith({ status: 'errored' }, { diverged: true }),
      wsWith({ status: 'planned' }),
      wsWith({ status: 'planned', 'plan-only': true }),
      wsWith(null, { drift: 'drifted' }),
    ]
    assert.deepEqual(runStatusCounts(list), { errored: 2, 'needs-confirm': 1, planned: 1 })
    for (const [status, n] of Object.entries(runStatusCounts(list))) {
      const matched = list.filter((w) => matches(`status:${status}`, w)).length
      assert.equal(matched, n, status)
    }
  })
})
