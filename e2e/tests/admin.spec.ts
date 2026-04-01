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
    await expect(userPage.locator('nav >> text=Agent Pools')).not.toBeVisible();
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
