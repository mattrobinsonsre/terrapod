// The run timeline's order is chronological, and `configuring-at` is why (#2334).
//
// The page renders `RUN_TIMELINE_KEYS` in sequence and filters out keys a run
// has no timestamp for, so this array's order is the ONLY thing deciding what
// appears above what. `configuring-at` was appended after `applied-at`, which
// is backwards for a composite run: `applied-at` is served as
// `configure_finished_at or apply_finished_at`, so on a composite it carries
// the moment the configure FINISHED -- later than `configuring-at`. The page
// showed "Applied" above "Configure started".
//
// Pinned as a constant with a test rather than fixed in place, because the
// defect was an append to a literal buried in JSX and the next append would
// land the same way.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { RUN_TIMELINE_KEYS } from '../src/lib/run-timeline.ts'

const at = (k: string) => {
  const i = RUN_TIMELINE_KEYS.indexOf(k)
  assert.notEqual(i, -1, `${k} is missing from RUN_TIMELINE_KEYS`)
  return i
}

test('a composite run reads in order: apply starts, configure starts, run finishes', () => {
  assert.ok(
    at('applying-at') < at('configuring-at'),
    'a configure starts after its apply starts',
  )
  assert.ok(
    at('configuring-at') < at('applied-at'),
    "`applied-at` is `configure_finished_at` on a composite, so it is LAST -- " +
      'this is the assertion the original order failed',
  )
})

test('the plan phases precede the apply phases', () => {
  for (const [earlier, later] of [
    ['queued-at', 'planning-at'],
    ['planning-at', 'planned-at'],
    ['planned-at', 'confirmed-at'],
    ['confirmed-at', 'applying-at'],
  ] as const) {
    assert.ok(at(earlier) < at(later), `${earlier} must precede ${later}`)
  }
})

test('the terminal keys come last, after every phase key', () => {
  const phases = ['queued-at', 'planning-at', 'planned-at', 'confirmed-at',
    'applying-at', 'configuring-at', 'applied-at']
  for (const terminal of ['errored-at', 'canceled-at', 'discarded-at']) {
    for (const phase of phases) {
      assert.ok(at(phase) < at(terminal), `${phase} must precede ${terminal}`)
    }
  }
})

test('`configured-at` is deliberately absent, not forgotten', () => {
  // On a composite it is the same instant `applied-at` already carries, so
  // rendering both would print one moment twice under two labels. If someone
  // adds it, this test is where the reason is written down.
  assert.ok(
    !RUN_TIMELINE_KEYS.includes('configured-at'),
    '`configured-at` duplicates `applied-at` on a composite; see run-timeline.ts',
  )
})

test('every key is a distinct timestamp attribute', () => {
  assert.equal(new Set(RUN_TIMELINE_KEYS).size, RUN_TIMELINE_KEYS.length)
  assert.ok(RUN_TIMELINE_KEYS.length >= 10, 'the array has been truncated')
  for (const k of RUN_TIMELINE_KEYS) assert.match(k, /-at$/)
})
