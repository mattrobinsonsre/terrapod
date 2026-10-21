// The run log pane sizes to the window (#2217).
//
// The property is NOT "nothing is cut off" — that passes on a tall window while
// the pane wastes a third of the screen, which was half of what was reported.
// The property is that the GAP below the pane does not move when the window
// does. These tests state it that way, and the second one shows the assertion
// can tell the new rule from the old: the `70vh` cap fails it in both
// directions.
//
// None of this applies on touch. Every sizing class on the pane is `fine:`
// gated, so on a coarse pointer the pane has no cap, no floor and no inner
// scroller: it grows to its content and the PAGE scrolls, which is the house
// touch rule (no inner scrollbar nested inside a scrolling page). A floor on a
// phone would only add dead space under a short log. That gating is a property
// of the component rather than of this module, so it is pinned separately in
// log-pane-touch.test.ts.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  LOG_PANE_BOTTOM_GAP_PX,
  LOG_PANE_MIN_PX,
  logPaneHeightPx,
} from '../src/lib/log-pane-height.ts'

/** Where the pane starts on a real run page: nav, back link, title, subtitle,
 *  actions, tab row and the log toolbar. Measured around 490px. */
const TOP = 490

const gap = (viewport: number, top = TOP) => viewport - top - logPaneHeightPx(top, viewport)

test('the gap below the pane is the same at every window height', () => {
  // The whole requirement, in one assertion.
  //
  // Scoped to the heights where the floor is NOT binding, because below that
  // the pane deliberately stops shrinking and the page scrolls instead -- the
  // next test covers that case. The threshold is DERIVED from the constants
  // rather than written as a literal, so changing the floor cannot silently
  // move a height from one property to the other.
  const candidates = [900, 1030, 1200, 1630, 2000, 2400]
  const heights = candidates.filter(
    (h) => h - TOP - LOG_PANE_BOTTOM_GAP_PX >= LOG_PANE_MIN_PX,
  )
  // A floor on the subject: a filter that matched nothing would make the loop
  // below vacuous, and it would still report as a pass.
  assert.ok(
    heights.length >= 4,
    `only ${heights.length} of ${candidates.length} heights clear the floor -- the assertion below is too weak to mean anything`,
  )
  for (const h of heights) {
    const g = gap(h)
    assert.equal(g, LOG_PANE_BOTTOM_GAP_PX, `viewport ${h} gave a gap of ${g}, not ${LOG_PANE_BOTTOM_GAP_PX}`)
  }
})

test('the old 70vh rule fails that assertion, in both directions', () => {
  // Not a test of production code — a check that the assertion above is capable
  // of failing, and of catching BOTH reported symptoms rather than only the
  // one that is visible on the reporter's own window.
  const oldHeight = (viewport: number) => 0.7 * viewport
  const oldGap = (viewport: number) => viewport - TOP - oldHeight(viewport)

  // Overflows on an ordinary window ...
  assert.ok(oldGap(1030) < 0, `expected the old rule to overflow at 1030, gap was ${oldGap(1030)}`)
  // ... and opens a growing band of dead space on a tall one.
  assert.ok(oldGap(2400) > LOG_PANE_BOTTOM_GAP_PX * 4, `expected dead space at 2400, gap was ${oldGap(2400)}`)
  // The two differ by a lot, which is exactly what "does not size to the
  // window" means and what a constant-gap assertion catches.
  assert.ok(Math.abs(oldGap(2400) - oldGap(1030)) > 400)
})

test('the floor is absolute pixels, so a short window scrolls rather than overflowing', () => {
  // A floor in `vh` is what made the old `min-h-[70vh]` worse than the cap: past
  // 30% of the window it cannot be satisfied inside the viewport at all, so it
  // guarantees the overflow. An absolute floor just means the page scrolls.
  const short = TOP + LOG_PANE_MIN_PX - 100
  assert.equal(logPaneHeightPx(TOP, short), LOG_PANE_MIN_PX)
  // And it does not scale with the window, which is the point.
  assert.equal(logPaneHeightPx(TOP, short * 2) > LOG_PANE_MIN_PX, true)
})

test('a taller page header takes height from the pane, not from the window', () => {
  // A wrapped title or an added banner moves the pane down. The pane must give
  // up exactly that much, so the gap is unchanged -- this is why the top is
  // measured rather than hardcoded.
  const viewport = 1200
  assert.equal(gap(viewport, 490), LOG_PANE_BOTTOM_GAP_PX)
  assert.equal(gap(viewport, 560), LOG_PANE_BOTTOM_GAP_PX)
  assert.equal(logPaneHeightPx(560, viewport), logPaneHeightPx(490, viewport) - 70)
})
