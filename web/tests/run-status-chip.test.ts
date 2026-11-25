import { test } from 'node:test'
import assert from 'node:assert/strict'

import { RUN_STATUSES, runStatusChipClass } from '../src/lib/workspace-status.ts'

// `statusColor` was written twice -- on the run-detail page and on the workspace
// page's runs list -- and the copies had diverged: `confirmed` and `queued` were
// handled on one and absent from the other, where they fell through to the same
// grey as `canceled` and `discarded`. So a run awaiting apply read as FINISHED
// on the workspace list and as awaiting one click later (#2715).
//
// The subject is derived from RUN_STATUSES rather than listed, so a new run
// status is covered the day it is declared there.

const FINISHED = 'bg-slate-700 text-slate-400'

// The statuses for which the finished grey is the CORRECT answer. Everything
// else must be distinguishable from them, which is the property that broke.
const LEGITIMATELY_GREY = new Set(['canceled', 'discarded'])

// A filter token that is not a raw run status, so no chip is rendered for it.
// It is a derived condition the pill resolves, not a value `run.status` takes.
const NOT_A_RUN_STATUS = new Set(['needs-confirm'])

test('no run status reads as finished unless it is finished', () => {
  const wrong: string[] = []
  for (const s of RUN_STATUSES) {
    if (LEGITIMATELY_GREY.has(s.filter) || NOT_A_RUN_STATUS.has(s.filter)) continue
    if (runStatusChipClass(s.filter) === FINISHED) wrong.push(s.filter)
  }
  assert.deepEqual(
    wrong,
    [],
    `these run statuses render the same grey as canceled/discarded, so they read as ` +
      `finished while the run is not: ${wrong.join(', ')}`,
  )
})

test('the two statuses that actually drifted are in-flight yellow', () => {
  // Named explicitly as well as covered by the sweep above, because these are
  // the regression: both were silently grey on the workspace page.
  const yellow = 'bg-yellow-900/50 text-yellow-300'
  assert.equal(runStatusChipClass('queued'), yellow)
  assert.equal(runStatusChipClass('confirmed'), 'bg-indigo-900/50 text-indigo-300')
})

test('the subject is not empty', () => {
  // A matcher that stops matching reports nothing. Equality, not a floor, so a
  // new run status is visible in the other direction too.
  assert.equal(RUN_STATUSES.length, 13)
})
