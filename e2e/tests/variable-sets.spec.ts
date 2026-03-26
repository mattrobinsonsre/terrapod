import { test, expect } from '@playwright/test';

test.describe('Variable Sets', () => {
  test('variable set list page loads', async ({ page }) => {
    await page.goto('/admin/variable-sets');
    await expect(page.locator('h1:has-text("Variable Sets")')).toBeVisible();
  });

  test('create global variable set appears with badge', async ({ page }) => {
    const name = `e2evs${Date.now()}`;

    await page.goto('/admin/variable-sets');
    await expect(page.locator('h1:has-text("Variable Sets")')).toBeVisible();

    // Toggle create form open
    await page.click('button:has-text("New Variable Set")');
    await page.fill('#vs-name', name);
    await page.fill('#vs-desc', 'E2E test set');

    // Check the Global checkbox
    const globalCheckbox = page.locator('label:has-text("Global") input[type="checkbox"]');
    await globalCheckbox.check();

    // Submit button says "Create Variable Set"
    await page.click('button[type="submit"]:has-text("Create Variable Set")');

    // Variable set should appear in the table with Global badge
    const row = page.locator(`tr:has-text("${name}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('text=Global')).toBeVisible();
  });

  test('navigate to detail page shows tabs', async ({ page }) => {
    const name = `e2evsdet${Date.now()}`;

    await page.goto('/admin/variable-sets');

    // Create a variable set
    await page.click('button:has-text("New Variable Set")');
    await page.fill('#vs-name', name);
    await page.fill('#vs-desc', 'Detail test');
    await page.click('button[type="submit"]:has-text("Create Variable Set")');

    // Click through to detail via the link in the row
    await page.click(`a:has-text("${name}")`);

    // Tabs should be visible
    await expect(page.getByRole('button', { name: 'Settings' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Variables' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Workspaces' })).toBeVisible();
  });

  test('add variable on detail page', async ({ page }) => {
    const name = `e2evsvar${Date.now()}`;
    const varKey = `E2E_VAR_${Date.now()}`;

    await page.goto('/admin/variable-sets');

    // Create variable set
    await page.click('button:has-text("New Variable Set")');
    await page.fill('#vs-name', name);
    await page.click('button[type="submit"]:has-text("Create Variable Set")');

    // Navigate to detail
    await page.click(`a:has-text("${name}")`);
    await expect(page.getByRole('button', { name: 'Variables' })).toBeVisible({ timeout: 10_000 });

    // Switch to Variables tab
    await page.getByRole('button', { name: 'Variables' }).click();

    // Add a variable
    await page.click('button:has-text("Add Variable")');
    await page.fill('#var-key', varKey);
    await page.fill('#var-val', 'test-value');
    await page.click('form button:has-text("Add Variable")');

    // Variable should appear in the table
    await expect(page.locator(`text=${varKey}`)).toBeVisible({ timeout: 10_000 });
  });

  test('delete variable set from detail page', async ({ page }) => {
    const name = `e2evsdel${Date.now()}`;

    await page.goto('/admin/variable-sets');

    // Create variable set to delete
    await page.click('button:has-text("New Variable Set")');
    await page.fill('#vs-name', name);
    await page.click('button[type="submit"]:has-text("Create Variable Set")');

    // Navigate to detail page
    await page.click(`a:has-text("${name}")`);
    await expect(page.getByRole('button', { name: 'Settings' })).toBeVisible({ timeout: 10_000 });

    // Settings tab should already be active; find Delete section
    await page.click('button:has-text("Delete")');

    // Click "Confirm Delete"
    await page.click('button:has-text("Confirm Delete")');

    // Should redirect back to list page and the varset should be gone
    await expect(page.locator('h1:has-text("Variable Sets")')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(`text=${name}`)).not.toBeVisible({ timeout: 5_000 });
  });
});
