import { test, expect } from '@playwright/test';
import { createWorkspace } from '../helpers/api.js';

const API_URL = process.env.API_URL || 'http://localhost:8000';

test.describe('Variables', () => {
  let workspaceId: string;
  const wsName = `e2e-vars-${Date.now()}`;

  test.beforeAll(async () => {
    // Create a workspace via API for variable tests.
    // Get admin token from the storageState file.
    const fs = await import('fs');
    const path = await import('path');
    const authPath = path.join(__dirname, '..', '.auth', 'admin.json');
    const authData = JSON.parse(fs.readFileSync(authPath, 'utf-8'));

    // Extract the session token from localStorage origins
    const origin = authData.origins?.find((o: { origin: string }) =>
      o.origin.includes('localhost'),
    );
    const authEntry = origin?.localStorage?.find(
      (e: { name: string }) => e.name === 'terrapod_auth',
    );
    const token = authEntry ? JSON.parse(authEntry.value).token : '';

    workspaceId = await createWorkspace(token, wsName);
  });

  test('create terraform variable', async ({ page }) => {
    await page.goto(`/workspaces/${workspaceId}?tab=variables`);

    // Click "Add Variable"
    await page.click('button:has-text("Add Variable")');

    // Fill in variable details
    await page.fill('#var-key', `TF_VAR_e2e_${Date.now()}`);
    await page.fill('#var-val', 'test-value');

    // Submit (the submit button also says "Add Variable")
    await page.click('form button:has-text("Add Variable")');

    // Variable should appear in the table
    await expect(page.locator('text=test-value')).toBeVisible({ timeout: 10_000 });
  });

  test('create sensitive variable shows masked value', async ({ page }) => {
    const varKey = `SECRET_e2e_${Date.now()}`;

    await page.goto(`/workspaces/${workspaceId}?tab=variables`);

    await page.click('button:has-text("Add Variable")');
    await page.fill('#var-key', varKey);
    await page.fill('#var-val', 'super-secret');

    // Check the Sensitive checkbox
    const sensitiveCheckbox = page.locator('label:has-text("Sensitive") input[type="checkbox"]');
    await sensitiveCheckbox.check();

    await page.click('form button:has-text("Add Variable")');

    // Value should be masked
    const row = page.locator(`tr:has-text("${varKey}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('text=***')).toBeVisible();
  });

  test('delete variable removes it from list', async ({ page }) => {
    const varKey = `DELETE_e2e_${Date.now()}`;

    await page.goto(`/workspaces/${workspaceId}?tab=variables`);

    // Create a variable to delete
    await page.click('button:has-text("Add Variable")');
    await page.fill('#var-key', varKey);
    await page.fill('#var-val', 'to-be-deleted');
    await page.click('form button:has-text("Add Variable")');

    // Wait for it to appear
    const row = page.locator(`tr:has-text("${varKey}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });

    // Delete it
    await row.locator('button:has-text("Delete")').click();

    // Should be gone
    await expect(row).not.toBeVisible({ timeout: 10_000 });
  });
});
