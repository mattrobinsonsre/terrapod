/**
 * The text of the confirmation for Apply and Discard.
 *
 * These are the irreversible tier, so they prompt in BOTH pointer modes (through
 * `useConfirm().confirmIrreversible`, the only place allowed to call
 * `window.confirm`). A native `confirm()` is unreadable past a handful of lines,
 * so the dialog carries the count and the first few names, then "and N more".
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { CONFIRM_NAME_LIMIT, confirmLines } from '../src/lib/workspace-bulk-confirm.ts'

const names = (n: number) => Array.from({ length: n }, (_, i) => `ws-${i + 1}`)

describe('confirmLines', () => {
  it('lists every name when the selection is short', () => {
    const lines = confirmLines(names(3))
    assert.deepEqual(lines.names, ['ws-1', 'ws-2', 'ws-3'])
    assert.equal(lines.more, 0)
    assert.equal(lines.count, 3)
  })

  it('caps the list and says how many more there are', () => {
    const lines = confirmLines(names(CONFIRM_NAME_LIMIT + 7))
    assert.equal(lines.names.length, CONFIRM_NAME_LIMIT)
    assert.equal(lines.more, 7)
    assert.equal(lines.count, CONFIRM_NAME_LIMIT + 7)
  })

  it('shows exactly the limit with no "more" line when it just fits', () => {
    const lines = confirmLines(names(CONFIRM_NAME_LIMIT))
    assert.equal(lines.more, 0)
  })

  it('keeps the first names in order, so the list is stable', () => {
    assert.deepEqual(confirmLines(names(50)).names.slice(0, 2), ['ws-1', 'ws-2'])
  })

  it('keeps the limit small enough to read in a native dialog', () => {
    assert.ok(CONFIRM_NAME_LIMIT >= 3 && CONFIRM_NAME_LIMIT <= 10)
  })
})
