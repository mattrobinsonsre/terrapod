import { test, expect } from '@playwright/test';

test.describe('Audit Log', () => {
  test('audit log page loads with entries', async ({ page }) => {
    await page.goto('/admin/audit-log');
    await expect(page.locator('h1:has-text("Audit Log")')).toBeVisible();

    // Previous test activity should have generated audit entries.
    // At minimum the table should have at least one row.
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });
  });

  test('apply method filter narrows results', async ({ page }) => {
    await page.goto('/admin/audit-log');
    await expect(page.locator('h1:has-text("Audit Log")')).toBeVisible();

    // Wait for initial data
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // Record initial row count
    const initialCount = await page.locator('table tbody tr').count();

    // Apply POST filter — should show fewer or equal rows
    await page.selectOption('#f-action', 'POST');
    await page.click('button:has-text("Apply Filters")');

    // Table should still be visible (there will be POST entries from test setup)
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // Every visible action badge should say POST
    const badges = page.locator('table tbody tr td:nth-child(3)');
    const count = await badges.count();
    for (let i = 0; i < count; i++) {
      await expect(badges.nth(i)).toContainText('POST');
    }
  });

  test('clear filters restores full list', async ({ page }) => {
    await page.goto('/admin/audit-log');
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // Apply a filter first
    await page.selectOption('#f-action', 'DELETE');
    await page.click('button:has-text("Apply Filters")');

    // Now clear
    await page.click('button:has-text("Clear")');

    // Table should reload with entries (broader set)
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // The method select should be reset (empty / "All" value)
    const val = await page.locator('#f-action').inputValue();
    expect(val).toBe('');
  });
});
