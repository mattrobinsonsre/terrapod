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
});
