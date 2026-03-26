import { test, expect } from '@playwright/test';

test.describe('Roles & Assignments', () => {
  test('roles tab shows built-in roles', async ({ page }) => {
    await page.goto('/admin/roles');
    await expect(page.locator('h1:has-text("Roles & Assignments")')).toBeVisible();

    // Built-in role headings should be visible (use h3 to avoid matching badges)
    await expect(page.locator('h3:has-text("admin")')).toBeVisible();
    await expect(page.locator('h3:has-text("audit")')).toBeVisible();
    await expect(page.locator('h3:has-text("everyone")')).toBeVisible();

    // At least one "built-in" badge should be present
    await expect(page.locator('text=built-in').first()).toBeVisible();
  });

  test('create custom role appears in list', async ({ page }) => {
    const roleName = `e2erole${Date.now()}`;

    await page.goto('/admin/roles');
    await expect(page.locator('h1:has-text("Roles & Assignments")')).toBeVisible();

    // Toggle create form open
    await page.click('button:has-text("Create Role")');

    await page.fill('#r-name', roleName);
    await page.selectOption('#r-perm', 'write');
    await page.fill('#r-desc', 'E2E test role');

    // Submit button says "Create Role"
    await page.click('button[type="submit"]:has-text("Create Role")');

    // Role should appear in the list
    await expect(page.locator(`h3:has-text("${roleName}")`)).toBeVisible({ timeout: 10_000 });
  });

  test('create assignment on assignments tab', async ({ page }) => {
    await page.goto('/admin/roles');

    // Switch to Assignments tab
    await page.click('button:has-text("Assignments")');

    // Toggle assignment form open
    await page.click('button:has-text("Add Assignment")');

    // Fill assignment form
    await page.selectOption('#a-provider', 'local');
    await page.fill('#a-email', 'e2e-user@terrapod.local');

    // Check the "audit" role checkbox
    const auditCheckbox = page.locator('label:has-text("audit") input[type="checkbox"]');
    await auditCheckbox.check();

    // Submit button says "Set Roles"
    await page.click('button[type="submit"]:has-text("Set Roles")');

    // Assignment should appear in the table
    await expect(
      page.locator('tr:has-text("e2e-user@terrapod.local")'),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('delete custom role removes it from list', async ({ page }) => {
    const roleName = `e2edel${Date.now()}`;

    await page.goto('/admin/roles');

    // Create the role first
    await page.click('button:has-text("Create Role")');
    await page.fill('#r-name', roleName);
    await page.selectOption('#r-perm', 'read');
    await page.fill('#r-desc', 'To be deleted');
    await page.click('button[type="submit"]:has-text("Create Role")');

    await expect(page.locator(`h3:has-text("${roleName}")`)).toBeVisible({ timeout: 10_000 });

    // Find the role card (a rounded-lg div containing the h3 with the role name)
    const card = page.locator('div.rounded-lg').filter({ has: page.locator(`h3:has-text("${roleName}")`) });
    await card.locator('button:has-text("Delete")').click();

    // Click Confirm (replaces Delete inline)
    await card.locator('button:has-text("Confirm")').click();

    // Role should be gone
    await expect(page.locator(`h3:has-text("${roleName}")`)).not.toBeVisible({ timeout: 10_000 });
  });
});
