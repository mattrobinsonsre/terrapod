import { test, expect } from '@playwright/test';
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api';

test.describe('Execution Hooks', () => {
  test('execution hooks list page loads', async ({ page }) => {
    await page.goto('/admin/execution-hooks');
    await expect(page.locator('h1:has-text("Execution Hooks")')).toBeVisible();
  });

  test('create hook appears in the list', async ({ page }) => {
    const name = `e2ehook${Date.now()}`;

    await page.goto('/admin/execution-hooks');
    await expect(page.locator('h1:has-text("Execution Hooks")')).toBeVisible();

    await page.click('button:has-text("New Execution Hook")');
    await page.fill('#hook-name', name);
    await page.fill('#hook-desc', 'E2E test hook');
    await page.selectOption('#hook-point', 'pre_plan');
    await page.fill('#hook-script', 'echo hi');
    await page.click('button[type="submit"]:has-text("Create Execution Hook")');

    const row = page.locator(`tr:has-text("${name}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('text=pre_plan')).toBeVisible();
    await expect(row.locator('text=Enabled')).toBeVisible();
  });

  test('detail page shows tabs, edit persists, delete removes', async ({ page }) => {
    const name = `e2ehookdet${Date.now()}`;

    await page.goto('/admin/execution-hooks');
    await page.click('button:has-text("New Execution Hook")');
    await page.fill('#hook-name', name);
    await page.selectOption('#hook-point', 'pre_init');
    await page.fill('#hook-script', 'true');
    await page.click('button[type="submit"]:has-text("Create Execution Hook")');

    await page.click(`a:has-text("${name}")`);
    await expect(page.getByRole('button', { name: 'Settings' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Workspaces' })).toBeVisible();

    // Edit: change hook point + priority, save, assert it persisted.
    await page.getByRole('button', { name: 'Edit' }).click();
    await page.getByRole('combobox').selectOption('post_apply');
    await page.getByRole('spinbutton').fill('7');
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.locator('text=Execution hook updated')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('dd:has-text("post_apply")')).toBeVisible();
    // Exact match: the name dd carries a timestamp that can also contain "7".
    await expect(page.locator('dd').filter({ hasText: /^7$/ })).toBeVisible();

    // Delete from the Settings tab.
    await page.click('button:has-text("Delete")');
    await page.click('button:has-text("Confirm Delete")');
    await expect(page.locator('h1:has-text("Execution Hooks")')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(`text=${name}`)).not.toBeVisible({ timeout: 5_000 });
  });

  test('associate a workspace, reflected in the list count', async ({ page }) => {
    const hookName = `e2ehookws${Date.now()}`;
    const wsName = uniqueName('e2e-hook-ws');
    const adminToken = getStoredToken();
    await createWorkspace(adminToken, wsName);

    await page.goto('/admin/execution-hooks');
    await page.click('button:has-text("New Execution Hook")');
    await page.fill('#hook-name', hookName);
    await page.selectOption('#hook-point', 'pre_init');
    await page.fill('#hook-script', 'true');
    await page.click('button[type="submit"]:has-text("Create Execution Hook")');

    await page.click(`a:has-text("${hookName}")`);
    await page.getByRole('button', { name: 'Workspaces' }).click();
    await page.click('button:has-text("Associate Workspace")');
    await page.selectOption('#hook-ws-select', { label: wsName });
    await page.click('form button:has-text("Add")');

    // The associated workspace row appears (name resolved) …
    await expect(page.locator(`tr:has-text("${wsName}")`)).toBeVisible({ timeout: 10_000 });

    // … and the list reflects workspace-count = 1 for this hook.
    await page.goto('/admin/execution-hooks');
    const row = page.locator(`tr:has-text("${hookName}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('td').last()).toHaveText('1');
  });
});
