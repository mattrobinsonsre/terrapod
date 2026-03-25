import { test, expect } from '@playwright/test';

test.describe('API Tokens', () => {
  test('token list page loads', async ({ page }) => {
    await page.goto('/settings/tokens');
    await expect(page.locator('h1:has-text("API Tokens")')).toBeVisible();
  });

  test('create token shows success banner with raw token', async ({ page }) => {
    await page.goto('/settings/tokens');

    // Toggle create form open
    await page.click('button:has-text("Create Token")');
    await page.fill('#tok-desc', `e2e-token-${Date.now()}`);

    // Submit button inside form says "Create"
    await page.click('button[type="submit"]:has-text("Create")');

    // Success banner should show the raw token
    await expect(page.locator('text=Token created successfully')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('code')).toBeVisible();
  });

  test('new token appears in table', async ({ page }) => {
    const desc = `e2e-list-${Date.now()}`;

    await page.goto('/settings/tokens');

    await page.click('button:has-text("Create Token")');
    await page.fill('#tok-desc', desc);
    await page.click('button[type="submit"]:has-text("Create")');

    // Wait for the token to appear in the table
    await expect(page.locator(`td:has-text("${desc}")`)).toBeVisible({ timeout: 10_000 });
  });

  test('revoke token removes it from table', async ({ page }) => {
    const desc = `e2e-revoke-${Date.now()}`;

    await page.goto('/settings/tokens');

    // Create a token to revoke
    await page.click('button:has-text("Create Token")');
    await page.fill('#tok-desc', desc);
    await page.click('button[type="submit"]:has-text("Create")');

    // Wait for it to appear
    const row = page.locator(`tr:has-text("${desc}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });

    // Revoke it
    await row.locator('button:has-text("Revoke")').click();

    // Should be removed
    await expect(row).not.toBeVisible({ timeout: 10_000 });
  });
});
