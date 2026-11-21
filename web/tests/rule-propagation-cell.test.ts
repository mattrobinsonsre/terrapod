/**
 * The propagation verdict cell (#2621 item 50).
 *
 * `rule_propagation_service` sends a per-setting diff in TWO shapes -- a
 * scalar's `{from, to}`, and a bare array of names for the two template
 * fields, whose specs hold secrets so only names are reportable. A helper
 * that absorbs "either shape" needs both pinned, or the test asserts
 * whichever one the fixture happened to use.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { propagationCell } from '../src/lib/rule-propagation.ts'

const EMPTY = '(none)'

describe('propagationCell', () => {
  it('renders both halves of the scalar {from, to} shape', () => {
    const diff: { from?: unknown; to?: unknown } = { from: 'advisory', to: 'mandatory' }
    assert.equal(propagationCell(diff.from, EMPTY), 'advisory')
    assert.equal(propagationCell(diff.to, EMPTY), 'mandatory')
  })

  it('renders the template-field shape as its names', () => {
    assert.equal(propagationCell(['nightly', 'on-apply'], EMPTY), 'nightly, on-apply')
  })

  it('renders an agent-pool id list, which arrives already prefixed', () => {
    assert.equal(propagationCell(['apool-1', 'apool-2'], EMPTY), 'apool-1, apool-2')
  })

  // The whole point of the `empty` parameter: a setting carrying nothing reads
  // as the caller's translated placeholder rather than as an empty cell, which
  // is indistinguishable from a rendering bug.
  it('uses the placeholder for every way a value can be absent', () => {
    for (const v of [null, undefined, '', []]) {
      assert.equal(propagationCell(v, EMPTY), EMPTY)
    }
  })

  // THE TRAP. `false` and `0` are falsy AND are real values of real settings
  // (auto-merge off; a zero interval). A guard written `if (!v) return empty`
  // passes every other test in this file and silently reports a setting that
  // is off as one that is unset -- a different fact, in the table an operator
  // reads before writing to their fleet.
  it('does not mistake a falsy value for an absent one', () => {
    assert.equal(propagationCell(false, EMPTY), 'false')
    assert.equal(propagationCell(true, EMPTY), 'true')
    assert.equal(propagationCell(0, EMPTY), '0')
  })

  it('renders a structured value rather than [object Object]', () => {
    assert.equal(propagationCell({ a: 1 }, EMPTY), '{"a":1}')
  })
})
