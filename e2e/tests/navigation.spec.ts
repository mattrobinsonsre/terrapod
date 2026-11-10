import { test, expect } from '@playwright/test';

test.describe('Navigation', () => {
  test('nav bar renders with Terrapod branding', async ({ page }) => {
    await page.goto('/workspaces');

    // Terrapod logo/brand should be visible in nav
    await expect(
      page.getByRole('navigation').getByRole('link', { name: /terrapod/i }).first(),
    ).toBeVisible();

    // Primary nav links stay visible on desktop (#719 IA); Modules/Providers
    // now live behind the Registry▾ dropdown, so assert the trigger instead.
    // By role, not by text. The bar renders an aria-hidden measurement copy
    // of itself (#1400) that is clipped to zero size, so a text locator finds
    // that first and never sees it. Role queries skip aria-hidden, and the
    // accessible name holds whether the bar is showing labels or icons.
    await expect(
      page.getByRole('navigation').getByRole('link', { name: 'Workspaces', exact: true }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: 'Registry' })).toBeVisible();
    await expect(
      page.getByRole('navigation').getByRole('link', { name: 'Catalog', exact: true }),
    ).toBeVisible();
  });

  test('navigate between pages', async ({ page }) => {
    // Start at workspaces
    await page.goto('/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Open Registry▾ and navigate to Modules
    await page.getByRole('button', { name: 'Registry' }).click();
    await page.getByRole('menuitem', { name: 'Modules' }).click();
    await page.waitForURL('**/registry/modules');
    await expect(page.locator('h1:has-text("Modules")')).toBeVisible();

    // Open Registry▾ again and navigate to Providers
    await page.getByRole('button', { name: 'Registry' }).click();
    await page.getByRole('menuitem', { name: 'Providers' }).click();
    await page.waitForURL('**/registry/providers');
    await expect(page.locator('h1:has-text("Providers")')).toBeVisible();

    // Navigate back to workspaces (top-level link)
    await page.getByRole('navigation')
      .getByRole('link', { name: 'Workspaces', exact: true }).click();
    await page.waitForURL('**/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();
  });

  test('account menu toggles the theme, labelled with what it will do, and survives a reload', async ({ page, context }) => {
    // The theme cookie is per-browser-context, so this test owns its own state.
    await context.clearCookies({ name: 'terrapod-theme' });
    await page.goto('/workspaces');
    const html = page.locator('html');
    await expect(html).not.toHaveClass(/\bdark\b/);

    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('menuitem', { name: 'Dark theme' }).click();
    await expect(html).toHaveClass(/\bdark\b/);
    // The menu stays open and the item now offers the way back.
    await expect(page.getByRole('menuitem', { name: 'Light theme' })).toBeVisible();

    await expect.poll(async () =>
      (await context.cookies()).find((c) => c.name === 'terrapod-theme')?.value,
    ).toBe('dark');
    // Server-rendered from the cookie: the raw HTML is dark before any client JS runs.
    const raw = await (await page.request.get('/workspaces')).text();
    expect(raw).toMatch(/<html[^>]*class="dark"/);
    await page.reload();
    await expect(html).toHaveClass(/\bdark\b/);

    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('menuitem', { name: 'Light theme' }).click();
    await expect(html).not.toHaveClass(/\bdark\b/);
  });
});
