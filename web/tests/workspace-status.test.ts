/**
 * `runStatusOf` is what `status:` filters on: the latest run's own status,
 * not the badge's resolved one, which lets a workspace-level condition win.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { RUN_STATUSES, resolveStatus, runStatusOf } from '../src/lib/workspace-status.ts'
import { matchWorkspace, parseFilterQuery } from '../src/lib/workspace-filter.ts'

function wsWith(
  run: { status: string; 'plan-only'?: boolean } | null,
  extra: { diverged?: boolean; vcsError?: string | null; drift?: string } = {},
) {
  return {
    attributes: {
      name: 'a',
      'state-diverged': extra.diverged ?? false,
      'vcs-last-error': extra.vcsError ?? null,
      'drift-status': extra.drift ?? '',
      'latest-run': run ? { id: 'run-1', 'plan-only': false, ...run } : null,
    },
  }
}

const matches = (q: string, w: ReturnType<typeof wsWith>) =>
  matchWorkspace(w, parseFilterQuery(q), runStatusOf(w))

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
  it('status:errored finds an errored run on a drifted, diverged or VCS-error workspace', () => {
    for (const extra of [{ drift: 'drifted' }, { diverged: true }, { vcsError: 'boom' }]) {
      const w = wsWith({ status: 'errored' }, extra)
      assert.equal(matches('status:errored', w), true, JSON.stringify(extra))
    }
  })

  it('the condition no longer reads as a status: status:drifted is not a run status', () => {
    const w = wsWith({ status: 'errored' }, { drift: 'drifted' })
    assert.equal(matches('status:drifted', w), false)
  })

  it('status:needs-confirm finds a planned run awaiting confirmation', () => {
    assert.equal(matches('status:needs-confirm', wsWith({ status: 'planned' })), true)
    assert.equal(matches('status:planned', wsWith({ status: 'planned' })), false)
  })

  it('a workspace with no latest run matches no status: value', () => {
    const w = wsWith(null, { drift: 'drifted' })
    for (const s of RUN_STATUSES) assert.equal(matches(`status:${s.filter}`, w), false, s.filter)
    assert.equal(matches('status:errored status:applied', w), false)
  })
})

describe('the page feeds the matcher and the dropdown counts from the same function', () => {
  const src = readFileSync(
    path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'app', 'workspaces', 'page.tsx'),
    'utf8',
  )

  it('no status: call site reads the badge', () => {
    assert.doesNotMatch(src, /resolveStatus\([^)]*\)\.def\?\.filter/)
    assert.equal((src.match(/runStatusOf\(ws\)/g) ?? []).length >= 2, true)
  })
})
