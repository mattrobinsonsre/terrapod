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

    // Apply the POST filter and wait for the *filtered* response to land before
    // asserting — otherwise the badge check races the table refresh and reads
    // the pre-filter (mixed-method) rows, which is what made this test flaky
    // (#240). Tying the assertion to the request the click triggers makes it
    // deterministic regardless of CI latency.
    await page.selectOption('#f-action', 'POST');
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/admin/audit-log') && r.url().includes('action') && r.ok()
      ),
      page.click('button:has-text("Apply Filters")'),
    ]);

    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // Assert there is no surviving row whose action cell is not POST. toHaveCount
    // auto-retries, so it keeps polling until the table has fully re-rendered
    // to the filtered set rather than snapshotting it once.
    const nonPost = page.locator('table tbody tr td:nth-child(3)').filter({ hasNotText: 'POST' });
    await expect(nonPost).toHaveCount(0, { timeout: 10_000 });
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
