import { test, expect } from '@playwright/test';

test.describe('Navigation', () => {
  test('nav bar renders with Terrapod branding', async ({ page }) => {
    await page.goto('/workspaces');

    // Terrapod logo/brand should be visible in nav
    await expect(page.locator('nav >> text=Terrapod').first()).toBeVisible();

    // Primary nav links stay visible on desktop (#719 IA); Modules/Providers
    // now live behind the Registry▾ dropdown, so assert the trigger instead.
    await expect(page.locator('nav >> text=Workspaces').first()).toBeVisible();
    await expect(page.getByRole('button', { name: 'Registry' })).toBeVisible();
    await expect(page.locator('nav >> text=Catalog').first()).toBeVisible();
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
    await page.locator('nav >> text=Workspaces').first().click();
    await page.waitForURL('**/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();
  });
});
