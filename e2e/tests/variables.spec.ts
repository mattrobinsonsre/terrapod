import { test, expect } from '@playwright/test';
import { createWorkspace } from '../helpers/api.js';

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

    // Variable should appear in the table. Scope to the <tr> — the value also
    // renders in the (hidden) mobile card, so a bare text= match is ambiguous.
    await expect(page.locator('tr:has-text("test-value")')).toBeVisible({ timeout: 10_000 });
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

  test('sensitive variable value is masked on entry, revealable', async ({ page }) => {
    await page.goto(`/workspaces/${workspaceId}?tab=variables`);

    await page.click('button:has-text("Add Variable")');
    const valField = page.locator('#var-val');
    await valField.fill('shoulder-surf-me');

    // Not sensitive yet -> value shown normally (no masking class).
    await expect(valField).not.toHaveClass(/text-masked/);

    // Mark sensitive -> the entry field masks (cross-browser disc font).
    await page.locator('label:has-text("Sensitive") input[type="checkbox"]').check();
    await expect(valField).toHaveClass(/text-masked/);

    // Show toggle reveals it; hiding masks again.
    await page.click('button[aria-label="Show value"]');
    await expect(valField).not.toHaveClass(/text-masked/);
    await page.click('button[aria-label="Hide value"]');
    await expect(valField).toHaveClass(/text-masked/);
  });

  test('git-auth category swaps the form to a credential builder and round-trips', async ({
    page,
  }) => {
    // #1028 / #1042: selecting a git category must ADAPT the form (the user's
    // complaint was that the generic value field stayed put), expose a
    // credential-source picker, and store a masked, URL-pattern-scoped var.
    const pattern = `github.com/e2e-${Date.now()}`;
    await page.goto(`/workspaces/${workspaceId}?tab=variables`);
    await page.click('button:has-text("Add Variable")');

    // Baseline: the generic value field is present for a normal category.
    await expect(page.locator('#var-val')).toBeVisible();

    // Switch to the git HTTPS credential category.
    await page.selectOption('#var-cat', 'git_http_auth');

    // The form ADAPTED: the generic value field is gone; the git builder is shown.
    await expect(page.locator('#var-val')).toHaveCount(0);
    await expect(page.locator('#git-source')).toBeVisible();

    // The key field is now the URL pattern; use a static token source so the
    // test needs no configured VCS connection.
    await page.fill('#var-key', pattern);
    await page.selectOption('#git-source', 'static');
    await page.fill('#git-token', 'ghp_e2e_fake_token');

    await page.click('form button:has-text("Add Variable")');

    // Row renders with the VISIBLE URL pattern (not secret) and a MASKED value
    // (the token is forced sensitive; it must never render in the table).
    const row = page.locator(`tr:has-text("${pattern}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('text=***')).toBeVisible();
    await expect(page.locator(`text=ghp_e2e_fake_token`)).toHaveCount(0);
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

    // Delete it — a native confirm() now guards the delete (#719); accept it.
    page.once('dialog', (d) => d.accept());
    await row.locator('button:has-text("Delete")').click();

    // Should be gone
    await expect(row).not.toBeVisible({ timeout: 10_000 });
  });
});
