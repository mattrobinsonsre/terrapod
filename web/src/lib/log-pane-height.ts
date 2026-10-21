/**
 * How tall the run log pane may be (#2217).
 *
 * The pane used to be capped at `70vh` — a fraction of the VIEWPORT rather than
 * of the space actually left below it. Because the pane sits well down the page
 * (nav bar, back link, title, subtitle, actions, tab row, toolbar) its bottom
 * landed at `top + 0.7 * viewport`, so the gap beneath it was
 *
 *     gap = viewport - (top + 0.7 * viewport) = 0.3 * viewport - top
 *
 * — a function of the window height rather than a constant. On a typical window
 * that is negative and the pane runs off the bottom; past roughly `top / 0.3`
 * it turns positive and opens a band of dead space that grows as the window
 * does. Resizing the window visibly changed the gap, which is what gave it
 * away.
 *
 * Here the height is the space that is actually left, so the gap is the page's
 * own bottom padding at every window size. That is what "sizes to the window"
 * means, and it is the property the test asserts: not that nothing is cut off,
 * but that the gap does not move.
 *
 * Extracted rather than written inline because it is the whole decision, and a
 * test that drives the real layout can only observe it through a browser.
 */

/**
 * The page's own bottom padding (`py-8` on `<main>`), so the pane stops where
 * the page would have stopped anyway rather than flush against the window.
 */
export const LOG_PANE_BOTTOM_GAP_PX = 32

/**
 * The floor, in PIXELS rather than a viewport fraction.
 *
 * A floor expressed in `vh` is what made the old `min-h-[70vh]` worse than the
 * cap: once the content above exceeds 30% of the window, a 70vh minimum cannot
 * be satisfied inside the viewport at all, so it *guarantees* the overflow
 * instead of merely permitting it. An absolute floor degrades honestly — on a
 * window too short to hold it the page scrolls, which is the right outcome.
 *
 * The pane is `text-sm` (20px line height) inside `p-4` (32px of vertical
 * padding), so the visible line count is `(height - 32) / 20`:
 *
 *   | floor | lines |
 *   |-------|-------|
 *   | 320px |  ~14  |
 *   | 400px |  ~18  |
 *   | 480px |  ~22  |
 *
 * 400px — about eighteen lines — is enough to read a stack trace or a plan
 * summary without it becoming a letterbox. Sibling precedent: the estate graph
 * uses `min-h-[420px]`.
 */
export const LOG_PANE_MIN_PX = 400

/**
 * @param top   the pane's offset from the top of the DOCUMENT, not the
 *              viewport. The nav is `sticky`, so it occupies layout space and
 *              this is stable under scrolling — a viewport-relative top would
 *              make the pane resize as the page moved.
 * @param viewportHeight `window.innerHeight`.
 */
export function logPaneHeightPx(top: number, viewportHeight: number): number {
  const available = viewportHeight - top - LOG_PANE_BOTTOM_GAP_PX
  return Math.max(LOG_PANE_MIN_PX, Math.round(available))
}
