import { test, expect } from '@playwright/test';
import path from 'path';

/**
 * RBAC negative paths — assert that non-admin identities are actually BLOCKED
 * from admin surfaces, not just that admins can reach them. This is the half
 * of the permission model that mocked unit tests can't prove at the UX layer:
 * a real non-admin session against the real app.
 *
 * Auth states are minted in global-setup:
 *   - user.json  → regular user (`everyone` role only)
 *   - audit.json → read-only `audit` role
 */
const USER_AUTH = path.join(__dirname, '..', '.auth', 'user.json');
const AUDIT_AUTH = path.join(__dirname, '..', '.auth', 'audit.json');

// Admin-only management surfaces (nav links gated on `isAdmin`).
const ADMIN_LINKS = [
  '/admin/users',
  '/admin/roles',
  '/admin/vcs-connections',
  '/admin/variable-sets',
  '/admin/binary-cache',
  '/admin/bulk-update',
];

test.describe('RBAC — regular user is blocked from admin', () => {
  test.use({ storageState: USER_AUTH });

  test('admin nav links are hidden for a regular user', async ({ page }) => {
    await page.goto('/workspaces');
    // Workspaces is reachable by everyone — confirms we're logged in.
    await expect(page.locator('a[href="/workspaces"]').first()).toBeVisible();
    // None of the admin management links should render.
    for (const href of ADMIN_LINKS) {
      await expect(page.locator(`a[href="${href}"]`)).toHaveCount(0);
    }
    // Audit log is gated on admin-OR-audit — also hidden for a plain user.
    await expect(page.locator('a[href="/admin/audit-log"]')).toHaveCount(0);
  });

  test('direct navigation to user management shows no admin write controls', async ({ page }) => {
    await page.goto('/admin/users');
    // A regular user must not get the management affordances. The API returns
    // 403, so the create/add control never renders.
    await expect(page.getByRole('button', { name: /add user|create user|new user/i })).toHaveCount(
      0,
    );
  });
});

test.describe('RBAC — audit user is read-only', () => {
  test.use({ storageState: AUDIT_AUTH });

  test('audit can reach the audit log but not the admin management links', async ({ page }) => {
    await page.goto('/workspaces');
    // Audit-or-admin gate → the audit log link IS visible to an audit user.
    await expect(page.locator('a[href="/admin/audit-log"]')).toBeVisible();
    // …but the admin-only management links are not.
    for (const href of ADMIN_LINKS) {
      await expect(page.locator(`a[href="${href}"]`)).toHaveCount(0);
    }
  });

  test('direct navigation to role management shows no admin write controls', async ({ page }) => {
    await page.goto('/admin/roles');
    // The audit role is read-only — role create/edit affordances must be absent
    // (the role API rejects non-admins, so the create control never renders).
    await expect(
      page.getByRole('button', { name: /create role|new role|add role/i }),
    ).toHaveCount(0);
  });
});
