import { expect, type Page } from '@playwright/test';

/**
 * Shared responsive assertions (#719).
 *
 * These live in a helper module rather than in `responsive.spec.ts` because
 * Playwright refuses to let one spec file import another — and the mobile
 * guard is meant to be reusable: any spec that adds a new surface should be
 * able to assert the mobile invariants on it without duplicating the check.
 */

/**
 * Asserts the page does not scroll horizontally at the current viewport —
 * the single most important mobile invariant. Allows a 1px rounding slack.
 */
export async function expectNoHorizontalPageScroll(page: Page) {
  const overflow = await page.evaluate(() => {
    const el = document.documentElement;
    return el.scrollWidth - el.clientWidth;
  });
  expect(
    overflow,
    `page scrolls horizontally by ${overflow}px at ${page.viewportSize()?.width}px viewport`,
  ).toBeLessThanOrEqual(1);
}
