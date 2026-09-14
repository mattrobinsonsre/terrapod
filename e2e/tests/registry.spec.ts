import { test, expect } from '@playwright/test';
import { getStoredToken, createRegistryModule } from '../helpers/api';

test.describe('Registry — Modules', () => {
  test('modules sharing a repository are grouped, with the submodule path shown (#1583)', async ({
    page,
  }) => {
    // A root module and a submodule of the same repository. Each run gets its
    // own repository URL, so parallel runs never share a group. Module names
    // are registry-safe: lowercase letters and digits, no hyphens.
    const token = getStoredToken();
    const stamp = Date.now().toString(36);
    const repo = `https://github.com/e2e-org/terraform-mg-${stamp}`;
    const root = `e2emgroot${stamp}`;
    await createRegistryModule(token, root, 'azurerm', { 'vcs-repo-url': repo });
    await createRegistryModule(token, `e2emgcreate${stamp}`, 'azurerm', {
      'vcs-repo-url': repo,
      subdirectory: 'modules/create',
    });

    await page.goto('/registry/modules');
    const group = page.locator('section', { has: page.getByRole('heading', { name: repo }) });
    await expect(group).toBeVisible({ timeout: 10_000 });
    await expect(group).toContainText('2 modules in this repository');
    await expect(group.getByText(root, { exact: true })).toBeVisible();
    await expect(group.getByText('Submodule: modules/create')).toBeVisible();
  });

  test('module list page loads', async ({ page }) => {
    await page.goto('/registry/modules');
    await expect(page.locator('h1:has-text("Modules")')).toBeVisible();
  });

  test('create module and see it in grid', async ({ page }) => {
    const name = `e2emod${Date.now()}`;
    const provider = 'aws';

    await page.goto('/registry/modules');
    await expect(page.locator('h1:has-text("Modules")')).toBeVisible();

    // Toggle create form open
    await page.click('button:has-text("Create Module")');

    await page.fill('#mod-name', name);
    await page.fill('#mod-provider', provider);

    // Submit button inside the form says "Create"
    await page.click('button[type="submit"]:has-text("Create")');

    // Module card should appear in the grid
    await expect(page.locator(`text=${name}`)).toBeVisible({ timeout: 10_000 });
  });

  test('click module navigates to detail page', async ({ page }) => {
    const name = `e2emoddet${Date.now()}`;
    const provider = 'aws';

    await page.goto('/registry/modules');
    await page.click('button:has-text("Create Module")');
    await page.fill('#mod-name', name);
    await page.fill('#mod-provider', provider);
    await page.click('button[type="submit"]:has-text("Create")');

    // Click the module card link
    await page.click(`text=${name}`);

    // Detail page should load with the module name in heading
    await expect(page.locator(`h1:has-text("${name}")`)).toBeVisible({ timeout: 10_000 });
  });
});

test.describe('Registry — Providers', () => {
  test('create provider and see it in grid', async ({ page }) => {
    const name = `e2eprov${Date.now()}`;

    await page.goto('/registry/providers');
    await expect(page.locator('h1:has-text("Providers")')).toBeVisible();

    await page.click('button:has-text("Create Provider")');
    await page.fill('#prov-name', name);
    await page.click('button[type="submit"]:has-text("Create")');

    await expect(page.locator(`text=${name}`)).toBeVisible({ timeout: 10_000 });
  });

  test('click provider navigates to detail page', async ({ page }) => {
    const name = `e2eprovdet${Date.now()}`;

    await page.goto('/registry/providers');
    await page.click('button:has-text("Create Provider")');
    await page.fill('#prov-name', name);
    await page.click('button[type="submit"]:has-text("Create")');

    await page.click(`text=${name}`);

    await expect(page.locator(`h1:has-text("${name}")`)).toBeVisible({ timeout: 10_000 });
  });
});
