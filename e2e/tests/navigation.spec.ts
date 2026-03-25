import { test, expect } from '@playwright/test';

test.describe('Navigation', () => {
  test('nav bar renders with Terrapod branding', async ({ page }) => {
    await page.goto('/workspaces');

    // Terrapod logo/brand should be visible in nav
    await expect(page.locator('nav >> text=Terrapod').first()).toBeVisible();

    // Core nav links should be visible
    await expect(page.locator('nav >> text=Workspaces').first()).toBeVisible();
    await expect(page.locator('nav >> text=Modules').first()).toBeVisible();
    await expect(page.locator('nav >> text=Providers').first()).toBeVisible();
  });

  test('navigate between pages', async ({ page }) => {
    // Start at workspaces
    await page.goto('/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Navigate to modules
    await page.locator('nav >> text=Modules').first().click();
    await page.waitForURL('**/registry/modules');
    await expect(page.locator('h1:has-text("Modules")')).toBeVisible();

    // Navigate to providers
    await page.locator('nav >> text=Providers').first().click();
    await page.waitForURL('**/registry/providers');
    await expect(page.locator('h1:has-text("Providers")')).toBeVisible();

    // Navigate back to workspaces
    await page.locator('nav >> text=Workspaces').first().click();
    await page.waitForURL('**/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();
  });
});
