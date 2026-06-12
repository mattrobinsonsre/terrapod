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

    // Success banner should show the raw token (shared by create + rotate).
    await expect(page.locator('text=Token ready')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('code')).toBeVisible();
  });

  test('new token appears in table with a Kind column', async ({ page }) => {
    const desc = `e2e-list-${Date.now()}`;

    await page.goto('/settings/tokens');

    await page.click('button:has-text("Create Token")');
    await page.fill('#tok-desc', desc);
    await page.click('button[type="submit"]:has-text("Create")');

    // Wait for the token to appear in the table, then the table exists so the
    // Kind column header (scoped-service-token surface, #495) is present.
    await expect(page.locator(`td:has-text("${desc}")`)).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('th:has-text("Kind")')).toBeVisible();
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

  test('create a service token, see its kind badge + rotate it (#495)', async ({ page }) => {
    const desc = `e2e-service-${Date.now()}`;

    await page.goto('/settings/tokens');
    await page.click('button:has-text("Create Token")');
    await page.fill('#tok-desc', desc);

    // Choose the bound service kind; the kind-specific help + role picker appear.
    await page.selectOption('#tok-kind', 'service_bound');
    await page.click('button[type="submit"]:has-text("Create")');

    // Banner + the new row carries the service badge.
    await expect(page.locator('text=Token ready')).toBeVisible({ timeout: 10_000 });
    const row = page.locator(`tr:has-text("${desc}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('text=Service · bound')).toBeVisible();

    // Service tokens expose a Rotate action that mints a fresh secret.
    page.on('dialog', (d) => d.accept());
    await row.locator('button:has-text("Rotate")').click();
    await expect(page.locator('text=Token ready')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('code')).toBeVisible();
  });

  test('admin All Tokens view exposes the kind filter + Bound To column (#495)', async ({ page }) => {
    // Seed a token so the table (and thus the Bound To header) renders.
    await page.goto('/settings/tokens');
    await page.click('button:has-text("Create Token")');
    await page.fill('#tok-desc', `e2e-all-${Date.now()}`);
    await page.click('button[type="submit"]:has-text("Create")');
    await expect(page.locator('text=Token ready')).toBeVisible({ timeout: 10_000 });

    // The All Tokens view is admin-only and exposes the kind filter + Bound To.
    await page.click('button:has-text("All Tokens")');
    await expect(page.locator('#kind-filter')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('th:has-text("Bound To")')).toBeVisible({ timeout: 10_000 });

    // Filtering by a valid-but-maybe-empty kind must not error the page.
    await page.selectOption('#kind-filter', 'service_detached');
    await expect(page.locator('h1:has-text("API Tokens")')).toBeVisible();
  });
});
