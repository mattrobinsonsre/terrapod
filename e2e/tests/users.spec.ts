import { test, expect } from '@playwright/test';

test.describe('User Management', () => {
  test('users page loads with admin user visible', async ({ page }) => {
    await page.goto('/admin/users');
    await expect(page.locator('h1:has-text("Users")')).toBeVisible();

    // Bootstrap admin should be in the table
    await expect(page.locator('text=admin@terrapod.local')).toBeVisible();
  });

  test('create new user appears in table', async ({ page }) => {
    const email = `e2ecreate${Date.now()}@terrapod.local`;

    await page.goto('/admin/users');
    await expect(page.locator('h1:has-text("Users")')).toBeVisible();

    // Toggle create form open
    await page.click('button:has-text("Create User")');
    await page.fill('#u-email', email);
    await page.fill('#u-name', 'E2E Created');
    await page.fill('#u-pw', 'StrongPass99!xyz');

    // Submit button says "Create User"
    await page.click('button[type="submit"]:has-text("Create User")');

    // New user should appear in the table (use td to avoid matching success banner)
    await expect(page.locator(`td:has-text("${email}")`)).toBeVisible({ timeout: 10_000 });
  });

  test('toggle user inactive changes badge', async ({ page }) => {
    const email = `e2etoggle${Date.now()}@terrapod.local`;

    await page.goto('/admin/users');

    // Create a user to toggle
    await page.click('button:has-text("Create User")');
    await page.fill('#u-email', email);
    await page.fill('#u-name', 'E2E Toggle');
    await page.fill('#u-pw', 'StrongPass99!xyz');
    await page.click('button[type="submit"]:has-text("Create User")');

    // Wait for user to appear in table (use td to avoid matching success banner)
    await expect(page.locator(`td:has-text("${email}")`)).toBeVisible({ timeout: 10_000 });

    // Find the row and click the Active badge button to toggle
    const row = page.locator(`tr:has-text("${email}")`);
    await row.locator('button:has-text("Active")').click();

    // Badge should change to Inactive
    await expect(row.locator('button:has-text("Inactive")')).toBeVisible({ timeout: 10_000 });
  });

  test('delete user removes from table', async ({ page }) => {
    const email = `e2edelete${Date.now()}@terrapod.local`;

    await page.goto('/admin/users');

    // Create a user to delete
    await page.click('button:has-text("Create User")');
    await page.fill('#u-email', email);
    await page.fill('#u-name', 'E2E Delete');
    await page.fill('#u-pw', 'StrongPass99!xyz');
    await page.click('button[type="submit"]:has-text("Create User")');

    const row = page.locator(`tr:has-text("${email}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });

    // Click Delete button in the row, then Confirm
    await row.locator('button:has-text("Delete")').click();
    await row.locator('button:has-text("Confirm")').click();

    // User should be gone
    await expect(row).not.toBeVisible({ timeout: 10_000 });
  });
});
