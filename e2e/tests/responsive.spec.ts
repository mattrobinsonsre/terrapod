import { test, expect, type Page } from '@playwright/test';
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api';

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

  test('nav adapts to mobile: hamburger shown, grouped sheet', async ({ page }) => {
    await page.goto('/workspaces');
    await expectNoHorizontalPageScroll(page);

    // The mobile branch of the nav renders a hamburger toggle; the desktop
    // link row is hidden below md. Proves the single nav component adapts
    // by width — no forked mobile build (#719).
    const hamburger = page.getByRole('button', { name: /open menu/i });
    await expect(hamburger).toBeVisible();

    // Opening it reveals the grouped sheet: primary links plus labelled
    // sections (Registry / Help, + Admin for admins). Account is NOT here — it
    // has its own trigger + drawer.
    await hamburger.click();
    const menu = page.locator('#mobile-nav-menu');
    await expect(menu).toBeVisible();
    await expect(menu.getByRole('link', { name: 'Workspaces' })).toBeVisible();
    await expect(menu.getByText('Registry', { exact: true })).toBeVisible();
    await expect(menu.getByText('Help', { exact: true })).toBeVisible();
    await expect(menu.getByRole('link', { name: 'Modules' })).toBeVisible();
    await expect(menu.getByText('Account', { exact: true })).toHaveCount(0);
    // Opening the sheet must not introduce horizontal overflow.
    await expectNoHorizontalPageScroll(page);

    // Account has its own trigger + drawer (personal/session items + log out).
    await menu.getByRole('button', { name: /close menu/i }).click();
    await page.getByRole('button', { name: 'Open account menu' }).click();
    const account = page.locator('#mobile-account-menu');
    await expect(account).toBeVisible();
    await expect(account.getByRole('link', { name: 'API Tokens' })).toBeVisible();
    await expect(account.getByRole('button', { name: 'Log out' })).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace list surfaces status in-row at phone width', async ({ page }) => {
    // Below `lg` the STATUS table column is hidden, so the row must carry an
    // inline status indicator — otherwise a phone loses the running/errored/
    // applied signal entirely (regression the mobile status line fixes, #719).
    const token = getStoredToken();
    const name = uniqueName('resp-status');
    await createWorkspace(token, name);

    // The client-side filter reads the `q` query param — narrow to our row.
    await page.goto(`/workspaces?q=${encodeURIComponent(name)}`);
    const row = page.getByRole('row').filter({ hasText: name });
    await expect(row).toBeVisible();

    // The inline mobile status indicator is present (a fresh workspace shows
    // "—", a run-bearing one shows its coloured pill — either way, not hidden).
    await expect(row.getByTestId('ws-row-status-mobile')).toBeVisible();

    // The dedicated desktop STATUS column header stays hidden at this width.
    await expect(page.getByRole('columnheader', { name: 'Status' })).toBeHidden();

    await expectNoHorizontalPageScroll(page);
  });

  test('workspace list trims secondary chrome at phone width', async ({ page }) => {
    // On a phone we drop the explanatory subtitle and the Total/Locked
    // stat cards (secondary), but KEEP Health Issues (the primary signal
    // that something needs attention) — #719.
    await page.goto('/workspaces');
    await expect(page.getByRole('heading', { name: 'Workspaces', level: 1 })).toBeVisible();

    await expect(page.getByText('Manage Terraform workspaces, state, and runs')).toBeHidden();
    // Compact stat chips: Total/Locked are desktop-only; Health always shows.
    await expect(page.getByText('Total', { exact: true })).toBeHidden();
    await expect(page.getByText('Locked', { exact: true })).toBeHidden();
    await expect(page.getByText('Health', { exact: true })).toBeVisible();

    await expectNoHorizontalPageScroll(page);
  });
});
