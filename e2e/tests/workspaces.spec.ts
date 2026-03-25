import { test, expect } from '@playwright/test';

test.describe('Workspaces', () => {
  test('workspace list page loads', async ({ page }) => {
    await page.goto('/workspaces');

    // Page header should be visible
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();
  });

  test('create workspace and see it in list', async ({ page }) => {
    const wsName = `e2e-ws-${Date.now()}`;

    await page.goto('/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Open create form
    await page.click('button:has-text("New Workspace")');

    // Fill in workspace name
    await page.fill('input[placeholder*="workspace"]', wsName);

    // Submit
    await page.click('button:has-text("Create Workspace")');

    // Workspace should appear in the list
    await expect(page.locator(`text=${wsName}`)).toBeVisible({ timeout: 10_000 });
  });

  test('workspace detail shows tabs', async ({ page }) => {
    // Create a workspace first
    const wsName = `e2e-detail-${Date.now()}`;

    await page.goto('/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');

    // Click on the workspace to go to detail
    await page.click(`text=${wsName}`);

    // Verify tabs are present (use getByRole to avoid matching text in other elements)
    await expect(page.getByRole('button', { name: 'Overview' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Variables' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Runs' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'State' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Notifications' })).toBeVisible();
  });

  test('workspace settings can be updated', async ({ page }) => {
    // Create a workspace
    const wsName = `e2e-settings-${Date.now()}`;

    await page.goto('/workspaces');
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');

    // Navigate to workspace detail
    await page.click(`text=${wsName}`);

    // Click Edit on the overview tab
    await page.click('button:has-text("Edit")');

    // Toggle auto-apply (it's a checkbox or toggle)
    const autoApplyToggle = page.locator('input[type="checkbox"]').first();
    const wasBefore = await autoApplyToggle.isChecked();
    await autoApplyToggle.click();

    // Save
    await page.click('button:has-text("Save")');

    // Wait for save to complete (Edit button re-appears)
    await expect(page.locator('button:has-text("Edit")')).toBeVisible({ timeout: 10_000 });

    // Reload and verify the toggle changed
    await page.reload();

    // Click Edit again to check the value
    await page.click('button:has-text("Edit")');
    const isAfter = await page.locator('input[type="checkbox"]').first().isChecked();
    expect(isAfter).not.toBe(wasBefore);
  });
});
