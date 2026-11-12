/**
 * #2551 -- the list must never render rows its own filter excludes, and the
 * `Showing N of M` figure must never disagree with the rows under it.
 *
 * The report was `Showing 2 of 530` above five rows, reproduced 5 times in 120
 * runs on an unmodified `main` build. These are the two invariants that make
 * that unrepresentable rather than merely unlikely, pinned where they can be
 * asserted deterministically -- a 5-in-120 observation is not a test.
 *
 * What was measured on the page while writing them, so the next reader does not
 * re-derive it:
 *
 *  - the label and the rows now come from the SAME array (`sortedWorkspaces`),
 *    so they cannot be given different derivations by a later edit. They were
 *    equal by construction before too -- `useSortable` returns
 *    `[...items].sort(...)`, which preserves length -- and the first invariant
 *    below is what keeps that true of the helper.
 *  - the render is an EXCLUSIVE ternary (loading / empty / filtered-empty /
 *    grouped / flat), so exactly one branch emits rows per commit.
 *  - the dangerous half of the report -- an operator selecting and acting on a
 *    workspace the filter excluded -- is already impossible on the page, and by
 *    construction rather than by the pruning effect: the bulk action's input is
 *    built by FILTERING the rendered array (`sortedWorkspaces.filter(w =>
 *    selected.has(w.id))`), so an id in `selected` that is not rendered cannot
 *    reach it. The second invariant below is what stops a refactor from passing
 *    `selected` straight through.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { pruneSelection, toggleAll, headerState } from '../src/lib/workspace-selection.ts'

const here = path.dirname(new URL(import.meta.url).pathname)
const sortableSrc = readFileSync(path.join(here, '..', 'src', 'lib', 'use-sortable.ts'), 'utf8')
const pageSrc = readFileSync(
  path.join(here, '..', 'src', 'app', 'workspaces', 'page.tsx'),
  'utf8',
)

/** The sort half of the page's derivation, exercised without React. */
function sortOf<T>(items: T[], accessor: (i: T, k: 'name') => string): T[] {
  // `useSortable` is a hook, so the pure property it must have -- length
  // preservation -- is asserted against the same algorithm it uses. Driving the
  // hook itself would need a renderer and would test React, not the invariant.
  return [...items].sort((a, b) =>
    String(accessor(a, 'name')).localeCompare(String(accessor(b, 'name')), undefined, {
      sensitivity: 'base',
    }),
  )
}

describe('#2551: the rendered count cannot disagree with the rendered rows', () => {
  it('sorting NEVER changes how many rows there are', () => {
    // The label reads `sortedWorkspaces.length` and the rows map the same
    // array, so this is the whole of the first invariant: whatever the sort
    // does, it must not add or drop a row. A `filter` smuggled into a sort
    // comparator, or a de-duplicating sort, would break it.
    const cases: string[][] = [
      [],
      ['only'],
      ['b', 'a'],
      ['a', 'a', 'a'], // duplicates must NOT be collapsed
      Array.from({ length: 530 }, (_, i) => `ws-${i}`),
    ]
    assert.ok(cases.length >= 5, 'the fixture set emptied')
    for (const names of cases) {
      const items = names.map((name, i) => ({ id: `id-${i}`, name }))
      const sorted = sortOf(items, (w) => w.name)
      assert.equal(sorted.length, items.length, `sort changed the count for ${items.length} items`)
      // And every input survives: a sort that dropped one and duplicated
      // another would keep the length while rendering the wrong rows.
      assert.deepEqual(
        sorted.map((w) => w.id).sort(),
        items.map((w) => w.id).sort(),
        'sort did not preserve the exact row set',
      )
    }
  })

  it('the LABEL counts the same array the rows map over', () => {
    // Structural on purpose. Pointing the label back at `filteredWorkspaces`
    // is a behavioural NO-OP today -- the two arrays are equal, which is why
    // the invariant above holds -- so no behavioural test can catch it, and an
    // unguarded fix of this shape undoes itself at the next edit. That is the
    // class #2551 is: a label fed by a different derivation from the rows.
    const label = pageSrc.match(/t\('filter\.showing',\s*\{\s*filtered:\s*([A-Za-z]+)\.length/)
    assert.ok(label, "could not find the `filter.showing` label's count expression")
    const rowsArray = pageSrc.match(/\{\s*([A-Za-z]+)\.map\(\(ws\) => \{/)
    assert.ok(rowsArray, 'could not find the array the table rows map over')
    assert.equal(
      label[1],
      rowsArray[1],
      `the label counts \`${label[1]}\` while the rows map \`${rowsArray[1]}\`. Two ` +
        'derivations for one number is how "Showing 2 of 530" appeared above five rows.',
    )
  })

  it('the hook really does sort a COPY, which is what makes the above its property', () => {
    // Read as source rather than imported: `use-sortable.ts` imports
    // `./view-prefs` extensionless, which Node's ESM loader will not resolve
    // from a test. The property that matters is structural anyway -- the hook
    // must spread into a new array and sort THAT, because `[...items].sort()`
    // is length-preserving while a `.filter(...).sort(...)` would not be, and
    // an in-place `items.sort()` would mutate the caller's array.
    assert.ok(
      /\[\.\.\.items\]\.sort\(/.test(sortableSrc),
      'useSortable no longer sorts a spread copy of `items` -- the length-preservation ' +
        'invariant above is no longer a property of the hook the page uses',
    )
    assert.ok(
      !/items\.filter\(/.test(sortableSrc),
      'useSortable now filters, so the label and the rows can disagree again (#2551)',
    )
    assert.ok(sortableSrc.length > 500, 'read an empty or truncated use-sortable.ts')
  })
})

describe('#2551: a selection can never reach a workspace that is not rendered', () => {
  const visible = ['a', 'b']
  const stale = new Set(['a', 'b', 'gone-1', 'gone-2'])

  it('the PAGE builds the bulk input by filtering the rendered array', () => {
    // Read off the page's own source, not replicated here. A copy of the
    // expression in this file would pass whatever the page does, which is the
    // whole failure mode this test exists to catch somewhere else.
    //
    // The shape required: the action's input is derived by filtering the
    // array the rows map over, so an id in `selected` that is not rendered
    // cannot reach a plan, apply, cancel or discard.
    const m = pageSrc.match(/const selectedBulk[\s\S]{0,400}?sortedWorkspaces\s*\n?\s*\.filter\(\s*\(?w\)?\s*=>\s*selected\.has\(w\.id\)\s*\)/)
    assert.ok(
      m,
      'the bulk action input is no longer `sortedWorkspaces.filter(w => selected.has(w.id))`. ' +
        'If it now reads `selected` directly, a workspace the filter excludes can be acted ' +
        'on -- which is what #2551 was reported as being dangerous about.',
    )
    // And `selected` must not be handed to the runner as the selection itself.
    //
    // The boundary matters and the first version of this line did not have it:
    // `selected` is a PREFIX of `selectedBulk`, so a bare `selected` pattern
    // flags the CORRECT call and the guard fails on a healthy page. Requiring a
    // delimiter right after it separates the two. Verified all four ways --
    // `selectedBulk` not flagged, bare `selected` flagged, `[...selected]`
    // flagged, `Array.from(selected)` not flagged (it is a copy of the set, so
    // it carries the same ghosts, but it cannot be written by accident).
    assert.ok(
      !/runBulkAction\(\s*action\s*,\s*(\[\.\.\.)?selected\s*[,)\]]/.test(pageSrc),
      'runBulkAction is being given `selected` rather than the rendered-and-filtered list',
    )
    assert.ok(pageSrc.length > 50_000, 'read an empty or truncated workspaces page')
  })

  it('the helpers behave, which is what the page composes', () => {
    const rendered = visible.map((id) => ({ id, name: id }))
    const forAction = rendered.filter((w) => stale.has(w.id))
    assert.deepEqual(forAction.map((w) => w.id), ['a', 'b'])
    for (const ghost of ['gone-1', 'gone-2']) {
      assert.ok(!forAction.some((w) => w.id === ghost), `${ghost} reached the action`)
    }
  })

  it('pruning is a backstop, and the set alone is NOT the guarantee', () => {
    // `pruneSelection` runs in an effect, i.e. AFTER the commit -- so for one
    // render `selected` legitimately holds ids that are no longer visible.
    // That is exactly why the filter-the-rendered-array form above is the
    // guarantee and this is only tidying.
    assert.deepEqual([...pruneSelection(new Set(stale), visible)].sort(), ['a', 'b'])
    // The pre-prune set really does contain ghosts, so the assertion above is
    // not vacuous.
    assert.ok(stale.has('gone-1'))
  })

  it('select-all selects exactly what is rendered', () => {
    assert.deepEqual([...toggleAll(new Set(), visible)].sort(), ['a', 'b'])
    // ...and clears from a partial selection rather than widening it.
    assert.equal(headerState(new Set(['a']), visible), 'some')
    assert.deepEqual([...toggleAll(new Set(['a']), visible)], [])
    assert.equal(headerState(new Set(['a', 'b']), visible), 'all')
    assert.equal(headerState(new Set(), visible), 'none')
  })
})
