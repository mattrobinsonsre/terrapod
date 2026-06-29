import { test, expect } from '../fixtures/auth.js';

test.describe('Admin access control', () => {
  test('admin user sees admin nav links', async ({ adminPage }) => {
    await adminPage.goto('/workspaces');
    await expect(adminPage.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Admin-only links should be visible
    await expect(adminPage.locator('nav >> text=Roles')).toBeVisible();
    await expect(adminPage.locator('nav >> text=Users')).toBeVisible();
    await expect(adminPage.locator('nav >> text=Agent Pools')).toBeVisible();
  });

  test('regular user does not see admin nav links', async ({ userPage }) => {
    await userPage.goto('/workspaces');
    await expect(userPage.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Admin-only links should not be visible
    await expect(userPage.locator('nav >> text=Roles')).not.toBeVisible();
    await expect(userPage.locator('nav >> text=Users')).not.toBeVisible();
    // Agent Pools is visible to all users (RBAC-filtered server-side)
    await expect(userPage.locator('nav >> text=Agent Pools')).toBeVisible();
  });

  test('admin can access roles page', async ({ adminPage }) => {
    await adminPage.goto('/admin/roles');

    await expect(adminPage.locator('h1:has-text("Roles")')).toBeVisible();
  });

  test('regular user is redirected from admin pages', async ({ userPage }) => {
    await userPage.goto('/admin/roles');

    // Should either show an error or redirect away from admin
    // The page checks auth and roles client-side — wait a moment for redirect
    await userPage.waitForTimeout(3_000);

    // Either redirected to home/workspaces or shows forbidden content
    const hasAdminContent = await userPage.locator('h1:has-text("Roles")').isVisible().catch(() => false);

    // Regular user should NOT see the admin roles content
    expect(hasAdminContent).toBe(false);
  });
});

test.describe('Cache bulk-warm (#606)', () => {
  test('admin can open the warm panel and a bad provider source surfaces an error', async ({ adminPage }) => {
    await adminPage.goto('/admin/binary-cache');
    await expect(adminPage.locator('h1:has-text("Cache")')).toBeVisible();

    // Expand the collapsible "Warm cache" panel.
    await adminPage.getByRole('button', { name: 'Warm cache' }).click();
    const providers = adminPage.locator('#warm-providers');
    await expect(providers).toBeVisible();

    // A provider source must be hostname/namespace/type — a bare name is
    // rejected (422) by the server before any upstream fetch, so this is
    // deterministic without egress.
    await providers.fill('aws 5.60.0');
    await adminPage.getByRole('button', { name: 'Warm', exact: true }).click();

    // The validation failure surfaces in the error banner.
    await expect(adminPage.locator('text=/Warm failed|hostname\\/namespace\\/type/i')).toBeVisible({
      timeout: 15_000,
    });
  });

  test('regular user is redirected from the cache admin page', async ({ userPage }) => {
    await userPage.goto('/admin/binary-cache');
    await userPage.waitForTimeout(3_000);
    const hasContent = await userPage
      .locator('h1:has-text("Cache")')
      .isVisible()
      .catch(() => false);
    expect(hasContent).toBe(false);
  });
});
