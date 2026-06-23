import { test, expect } from '@playwright/test';
import path from 'path';
import { getStoredToken, uniqueName, createRegistryModule } from '../helpers/api';

/**
 * Service Catalog (#535) — drives the real catalog UI through the BFF proxy
 * chain against the real stack: admin management (provider templates + catalog
 * items), the browse + provision surfaces, and the catalog-RBAC negative
 * (a user with no catalog grant sees an empty catalog even when items exist).
 *
 * The full provision→apply→destroy→archive flow needs a real runner and is
 * covered by the live Tilt smoke (the release F-gate), not here. The
 * instance-row lifecycle actions (reconfigure / destroy / the admin-gated
 * Orphan… escape hatch) likewise need a provisioned instance, so they are
 * exercised in that live smoke rather than this stack.
 */

const USER_AUTH = path.join(__dirname, '..', '.auth', 'user.json');

test.describe('Service Catalog — admin + browse (admin session)', () => {
  test('provider template: create and see it listed', async ({ page }) => {
    const name = uniqueName('e2e-pt');
    await page.goto('/admin/provider-templates');
    await expect(page.locator('h1:has-text("Provider Templates")')).toBeVisible();

    await page.click('button:has-text("New Template")');
    await page.fill('#pt-name', name);
    await page.fill('#pt-type', 'aws');
    await page.fill('#pt-body', 'provider "aws" {}');
    await page.click('button[type="submit"]:has-text("Create Template")');

    // Scope to the list row — a page-wide text match also hits the
    // "Provider template "<name>" created" success banner (strict-mode: 2 elements).
    await expect(page.locator(`td:has-text("${name}")`)).toBeVisible({ timeout: 10_000 });
  });

  test('catalog item: create over a module, browse, render provision panel', async ({ page }) => {
    const token = getStoredToken('admin.json');
    // Module names are restricted to a registry-safe charset — no hyphens.
    const moduleName = `e2ecatmod${Date.now().toString(36)}`;
    const moduleId = await createRegistryModule(token, moduleName, 'aws');
    const itemName = uniqueName('e2ecat');

    // Admin create
    await page.goto('/admin/catalog');
    await expect(page.locator('h1:has-text("Catalog Admin")')).toBeVisible();
    await page.click('button:has-text("New Catalog Item")');
    await page.fill('#cat-name', itemName);
    // Select the module by its UUID value (robust against label formatting).
    await page.selectOption('#cat-module', moduleId);
    await page.click('button[type="submit"]:has-text("Create Catalog Item")');
    // List row, not a page-wide text match (avoids also hitting the success banner).
    await expect(page.locator(`td:has-text("${itemName}")`)).toBeVisible({ timeout: 10_000 });

    // Browse — the enabled item appears
    await page.goto('/catalog');
    await expect(page.locator('h1:has-text("Service Catalog")')).toBeVisible();
    await expect(page.locator(`text=${itemName}`)).toBeVisible({ timeout: 10_000 });

    // Detail — provision panel renders
    await page.click(`a:has-text("${itemName}")`);
    await expect(page.locator('h2:has-text("Provision")')).toBeVisible({ timeout: 10_000 });
    // Selection-first: the agent-pool select is present (not a free-text field).
    await expect(page.locator('select').first()).toBeVisible();
  });
});

test.describe('Service Catalog — RBAC negative (regular user)', () => {
  test.use({ storageState: USER_AUTH });

  test('a user with no catalog grant sees an empty catalog', async ({ page }) => {
    await page.goto('/catalog');
    await expect(page.locator('h1:has-text("Service Catalog")')).toBeVisible();
    // Even though admin items exist in the shared DB, catalog RBAC filters them
    // out for a user with no catalog permission — the page shows the empty
    // state, never another user's item.
    await expect(page.locator('text=No catalog items available.')).toBeVisible({
      timeout: 10_000,
    });
  });
});
