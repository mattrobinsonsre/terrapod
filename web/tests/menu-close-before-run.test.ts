/**
 * A menu item closes the menu BEFORE it runs its action, on every menu.
 *
 * Both orders are invisible to a behavioural test: React batches state updates
 * inside an event handler, so the menu is gone by the time anything can look
 * either way round. An e2e comment claimed the order was what it asserted, and
 * swapping the two lines left it green (#2632).
 *
 * The order is still load-bearing, twice over. If the action throws, a `close`
 * written after it never runs and the menu is left open over whatever the error
 * did. And `close(true)` is what returns focus to the trigger — `setOpen(false)`
 * alone drops it to `<body>`, which is the defect this gate exists to keep
 * fixed, and which was still live in the split button after the selection menu
 * was repaired (#2621).
 *
 * So it is pinned positionally, in the source, which is the only place the
 * property is visible.
 */

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'components')

/** Each menu, and the call its item makes to run the chosen action. */
const MENUS: Array<{ file: string; run: string }> = [
  { file: 'selection-menu.tsx', run: 'it.run()' },
  { file: 'run-kind-split-button.tsx', run: 'o.run()' },
]

/**
 * Line comments removed, so prose about the code is not read as the code.
 *
 * Not politeness: the comment in `run-kind-split-button.tsx` explaining why the
 * close comes first QUOTES the run call, so the first match in the file was the
 * explanation rather than the statement -- and it sits above `close(true)`, so
 * the gate reported the defect it had just been written to confirm was fixed.
 */
function withoutComments(source: string): string {
  return source
    .split('\n')
    .map((line) => (line.trimStart().startsWith('//') ? '' : line))
    .join('\n')
}

/** The `onClick={() => { ... }}` body containing `run`, with its braces balanced. */
function handlerAround(rawSource: string, run: string): string {
  const source = withoutComments(rawSource)
  const at = source.indexOf(run)
  assert.notEqual(at, -1, `the run call ${run} is not in this file any more`)
  const open = source.lastIndexOf('onClick={() => {', at)
  assert.notEqual(open, -1, `${run} is not inside an onClick handler any more`)
  let depth = 0
  for (let i = source.indexOf('{', open + 'onClick='.length); i < source.length; i++) {
    if (source[i] === '{') depth += 1
    else if (source[i] === '}') {
      depth -= 1
      if (depth === 0) return source.slice(open, i + 1)
    }
  }
  throw new Error(`unbalanced onClick handler around ${run}`)
}

describe('a menu item closes before it runs', () => {
  for (const { file, run } of MENUS) {
    it(`${file} closes with focus returned, and does it first`, () => {
      const body = handlerAround(readFileSync(join(SRC, file), 'utf8'), run)

      const close = body.indexOf('close(true)')
      assert.notEqual(
        close,
        -1,
        `${file}'s menu item does not call close(true); setOpen(false) alone leaves ` +
          'focus on <body> and the keyboard user loses their place in the page',
      )
      const runAt = body.indexOf(run)
      assert.ok(
        close < runAt,
        `${file} runs the action before closing: if ${run} throws, the close never ` +
          'happens and the menu is left open over the error',
      )
    })
  }

  it('a comment quoting the run call does not stand in for it', () => {
    const fake = [
      '              onClick={() => {',
      '                // if `o.run()` throws, the close never happens',
      '                close(true)',
      '                o.run()',
      '              }}',
    ].join('\n')
    const body = handlerAround(fake, 'o.run()')
    assert.ok(
      body.indexOf('close(true)') < body.indexOf('o.run()'),
      'the comment quoting the run call was read as the call itself',
    )
  })

  it('the handler finder reads a balanced body and not the rest of the file', () => {
    // Its own fixtures. A finder that over-reached would scoop up a later
    // `close(true)` from another handler and pass whatever the real order is.
    const fake = [
      'x',
      '              onClick={() => {',
      '                if (!a) return',
      '                o.run()',
      '              }}',
      '              onKeyDown={() => close(true)}',
    ].join('\n')
    const body = handlerAround(fake, 'o.run()')
    assert.ok(body.includes('o.run()'))
    assert.ok(!body.includes('onKeyDown'), 'the finder ran past the end of the handler')
    assert.ok(!body.includes('close(true)'), 'the finder swallowed a sibling handler')
  })

  it('covers every menu in the tree', () => {
    assert.equal(MENUS.length, 2)
  })
})
