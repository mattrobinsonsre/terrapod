/**
 * `explainingPhase` (#2413 F16).
 *
 * The cases are the measured ones plus the two that are correct by
 * construction, and the file says which is which — a reasoned case asserted
 * beside a measured one, with nothing marking the difference, is how a
 * reasoned case comes to be cited as evidence.
 *
 * The one that matters most is `errored` with `configured-at` PRESENT: the
 * obvious rule ("the phase whose end timestamp is missing") returns nothing
 * there, and three of three real errored runs look like that.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { explainingPhase } from '../src/lib/run-phases.ts'

describe('explainingPhase', () => {
  // ── measured against real runs on a live instance ──────────────────────

  it('a planned run explains itself with its plan', () => {
    assert.equal(
      explainingPhase({ 'plan-queued-at': 'T', 'planning-at': 'T', 'planned-at': 'T' }),
      'plan',
    )
  })

  it('an applied composite explains itself with its configure', () => {
    assert.equal(
      explainingPhase({
        'plan-queued-at': 'T',
        'configuring-at': 'T',
        'applied-at': 'T',
        'configured-at': 'T',
      }),
      'configure',
    )
  })

  it('an ERRORED configure still carries configured-at, and is still the configure', () => {
    // The case that rules out the end-timestamp rule. `configure_finished_at`
    // is set when the configure finishes, successfully or not, so an
    // "unfinished phase" search finds nothing here.
    assert.equal(
      explainingPhase({
        'plan-queued-at': 'T',
        'configuring-at': 'T',
        'applied-at': 'T',
        'configured-at': 'T',
      }),
      'configure',
    )
  })

  it('a standalone configure has no planning-at and is not read as a plan', () => {
    // Measured: a configure-only run reports `applied-at` without ever having
    // planned, because that key is `configure_finished_at or apply_finished_at`.
    const r = explainingPhase({ 'plan-queued-at': 'T', 'configuring-at': 'T', 'applied-at': 'T' })
    assert.equal(r, 'configure')
    assert.notEqual(r, 'plan')
  })

  it('a canceled run that never started a phase explains nothing', () => {
    assert.equal(explainingPhase({}), null)
  })

  // ── correct by construction, NOT measured: the sample held neither ─────

  it('a run that errored in its plan never entered applying, so it is the plan', () => {
    assert.equal(explainingPhase({ 'plan-queued-at': 'T', 'planning-at': 'T' }), 'plan')
  })

  it('a run that errored in its apply is the apply', () => {
    assert.equal(
      explainingPhase({
        'plan-queued-at': 'T',
        'planning-at': 'T',
        'planned-at': 'T',
        'applying-at': 'T',
      }),
      'apply',
    )
  })

  // ── the shape of the inputs, which the server decides ──────────────────

  it('absent timestamps are a real answer, not a crash', () => {
    assert.equal(explainingPhase(null), null)
    assert.equal(explainingPhase(undefined), null)
  })

  it('the LAST started phase wins, not the first', () => {
    // The whole property in one case: every start present, and the answer is
    // the furthest the run got.
    assert.equal(
      explainingPhase({ 'planning-at': 'T', 'applying-at': 'T', 'configuring-at': 'T' }),
      'configure',
    )
    assert.equal(explainingPhase({ 'planning-at': 'T', 'applying-at': 'T' }), 'apply')
    assert.equal(explainingPhase({ 'planning-at': 'T' }), 'plan')
  })

  it('a falsy timestamp reads as absent, matching the server omitting it', () => {
    assert.equal(explainingPhase({ 'planning-at': 'T', 'configuring-at': '' }), 'plan')
    assert.equal(explainingPhase({ 'planning-at': 'T', 'configuring-at': null }), 'plan')
  })
})
