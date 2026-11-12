/**
 * The selection behind the Workspaces bulk actions.
 *
 * The header checkbox IS the clear: with nothing selected it selects every
 * visible row, and with anything selected, including a partial selection, it
 * clears. One click out from any state, so no separate Clear control exists.
 *
 * Selection is by workspace id and survives a filter or sort change, but a
 * workspace that has left the list (deleted, or no longer visible after a
 * refresh) is dropped, so an action can never run on something the user can no
 * longer see.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  headerState,
  pruneSelection,
  toggleAll,
  toggleOne,
} from '../src/lib/workspace-selection.ts'

const visible = ['a', 'b', 'c']

describe('headerState', () => {
  it('is unchecked with nothing selected', () => {
    assert.equal(headerState(new Set(), visible), 'none')
  })

  it('is indeterminate with some selected', () => {
    assert.equal(headerState(new Set(['a']), visible), 'some')
  })

  it('is checked when every visible row is selected', () => {
    assert.equal(headerState(new Set(['a', 'b', 'c']), visible), 'all')
  })

  it('counts only visible rows: a selected id that is not visible does not make it "all"', () => {
    assert.equal(headerState(new Set(['a', 'gone']), visible), 'some')
  })

  it('is unchecked for an empty list rather than vacuously checked', () => {
    assert.equal(headerState(new Set(), []), 'none')
  })
})

describe('toggleAll: the header is the clear', () => {
  it('selects every visible row from nothing', () => {
    assert.deepEqual([...toggleAll(new Set(), visible)].sort(), ['a', 'b', 'c'])
  })

  it('clears from a partial selection, not completes it', () => {
    assert.deepEqual([...toggleAll(new Set(['a']), visible)], [])
  })

  it('clears from a full selection', () => {
    assert.deepEqual([...toggleAll(new Set(['a', 'b', 'c']), visible)], [])
  })

  it('does not mutate the set it was given', () => {
    const before = new Set(['a'])
    toggleAll(before, visible)
    assert.deepEqual([...before], ['a'])
  })
})

describe('toggleOne', () => {
  it('adds an unselected id and removes a selected one', () => {
    assert.deepEqual([...toggleOne(new Set(), 'a')], ['a'])
    assert.deepEqual([...toggleOne(new Set(['a']), 'a')], [])
  })

  it('does not mutate the set it was given', () => {
    const before = new Set(['a'])
    toggleOne(before, 'b')
    assert.deepEqual([...before], ['a'])
  })
})

describe('pruneSelection', () => {
  it('drops ids that are no longer in the list', () => {
    assert.deepEqual([...pruneSelection(new Set(['a', 'gone']), visible)], ['a'])
  })

  it('returns the very same set when nothing was dropped, so a render does not loop', () => {
    const same = new Set(['a', 'b'])
    assert.equal(pruneSelection(same, visible), same)
  })
})
