import { test, expect, type Page } from '@playwright/test';

/**
 * Responsive / mobile harness (#719).
 *
 * This project runs at a phone viewport (see the `responsive` project in
 * playwright.config.ts — a Pixel device descriptor). It is the "mobile"
 * half of the two-sided testing contract: this suite proves the UI works
 * at phone width, while the existing desktop projects prove the desktop
 * view is unchanged (the desktop guard). One DRY UI, adapted by width —
 * never a forked mobile build, never user-agent sniffing.
 *
 * Per-page assertions (no horizontal page scroll, tables reflow, tab
 * survives reload, log tail visible, …) are added to this suite as each
 * stage of #719 fixes the corresponding surface, so the guard grows with
 * the work and can't silently regress.
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

test.describe('Responsive harness (phone viewport)', () => {
  test('runs at a phone viewport', async ({ page }) => {
    const vp = page.viewportSize();
    expect(vp, 'responsive project must set a viewport').not.toBeNull();
    expect(vp!.width, 'responsive project runs below the md breakpoint').toBeLessThan(768);
  });

  test('nav adapts to mobile: hamburger shown, desktop bar hidden', async ({ page }) => {
    await page.goto('/workspaces');

    // The mobile branch of the nav renders a hamburger toggle; the desktop
    // link row is hidden below md. Proves the single nav component adapts
    // by width (the foundation the Stage 1 nav restructure builds on).
    const hamburger = page.getByRole('button', { name: /open menu/i });
    await expect(hamburger).toBeVisible();

    // Opening it reveals the nav destinations (currently the full list;
    // Stage 1 groups these).
    await hamburger.click();
    await expect(page.locator('#mobile-nav-menu')).toBeVisible();
    await expect(page.locator('#mobile-nav-menu >> text=Workspaces').first()).toBeVisible();
  });
});
