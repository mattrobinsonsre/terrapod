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

  test('discover proposes a repository\'s modules and registers the ticked ones with their subdirectory (#1584)', async ({
    page,
  }) => {
    // The e2e stack has no repository to read, so the VCS connection and the
    // scan are stubbed, and so is the create call: the test asserts what the
    // page sends for the candidate it registers.
    const repo = 'https://github.com/e2e-org/terraform-azurerm-mg';
    await page.route(
      (url) => url.pathname === '/api/terrapod/v1/vcs-connections',
      (route) =>
        route.fulfill({
          json: {
            data: [
              { id: 'vcs-e2e', type: 'vcs-connections', attributes: { name: 'e2e-github', provider: 'github' } },
            ],
            meta: { pagination: { 'current-page': 1, 'page-size': 1, 'total-count': 1, 'total-pages': 1 } },
          },
        }),
    );
    // The panel scans through an unsaved module autodiscovery rule's preview.
    let scanned: Record<string, unknown> | null = null;
    await page.route(
      (url) => url.pathname === '/api/terrapod/v1/module-autodiscovery-rules/preview',
      async (route) => {
        scanned = route.request().postDataJSON().data.attributes;
        await route.fulfill({
          json: {
            data: {
              type: 'module-autodiscovery-rule-previews',
              attributes: {
                ref: 'main',
                'files-walked': 3,
                entries: [
                  {
                    subdirectory: '',
                    name: 'mg',
                    provider: 'azurerm',
                    'registered-as': { name: 'mg', provider: 'azurerm' },
                    collision: false,
                    'missing-provider': false,
                  },
                  {
                    subdirectory: 'modules/create',
                    name: 'mg-create',
                    provider: 'azurerm',
                    'registered-as': null,
                    collision: false,
                    'missing-provider': false,
                  },
                ],
              },
            },
          },
        });
      },
    );
    const created: Record<string, unknown>[] = [];
    await page.route(
      (url) => url.pathname === '/api/terrapod/v1/registry-modules',
      async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        created.push(route.request().postDataJSON().data.attributes);
        await route.fulfill({
          status: 201,
          json: { data: { id: 'mod-e2e', type: 'registry-modules', attributes: {} } },
        });
      },
    );

    await page.goto('/registry/modules');
    // The toggle is a client component: retry a click lost before hydration,
    // clicking only while the panel is still closed.
    const panel = page.getByRole('heading', { name: 'Discover modules in a repository' });
    await expect(async () => {
      if (!(await panel.isVisible())) await page.getByRole('button', { name: 'Discover modules' }).click();
      await expect(panel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });

    await page.getByLabel('VCS connection').selectOption('vcs-e2e');
    await page.getByLabel('Repository URL').fill(repo);
    await page.getByRole('button', { name: 'Scan repository' }).click();

    await expect(page.getByText('Already registered as mg/azurerm')).toBeVisible();
    await expect(page.getByRole('checkbox', { name: 'Register the module in (repository root)' })).toBeDisabled();
    expect(scanned).toMatchObject({ 'vcs-connection-id': 'vcs-e2e', 'repo-url': repo });

    await page.getByRole('checkbox', { name: 'Register the module in modules/create' }).check();
    const row = page.locator('li', { hasText: 'modules/create' });
    await row.getByLabel('Name').fill('mgcreate');
    await page.getByRole('button', { name: 'Register 1 module' }).click();

    await expect.poll(() => created.length).toBe(1);
    expect(created[0]).toMatchObject({
      name: 'mgcreate',
      provider: 'azurerm',
      subdirectory: 'modules/create',
      'vcs-connection-id': 'vcs-e2e',
      'vcs-repo-url': repo,
    });
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
