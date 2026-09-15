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
  '/admin/execution-hooks',
  '/admin/binary-cache',
  '/admin/bulk-update',
  '/admin/catalog',
  '/admin/provider-templates',
  // The most sensitive new admin page was the one not covered (#1297): a
  // delete marker names a workspace and its variable names, and a restore
  // materialises its state — and therefore its secrets — into a workspace
  // the caller can then read.
  '/admin/deleted-workspaces',
  // A rule reads repositories with the platform's VCS credentials and
  // registers modules on its own authority (#1584).
  '/admin/module-autodiscovery',
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

  test('direct navigation to module autodiscovery is turned away (#1584)', async ({ page }) => {
    // Every rules endpoint is admin-only, and the page sends a non-admin home
    // before it renders anything: no rules list, no create form, no scan.
    let rulesRequested = false;
    page.on('request', (req) => {
      if (new URL(req.url()).pathname.startsWith('/api/terrapod/v1/module-autodiscovery-rules')) {
        rulesRequested = true;
      }
    });
    await page.goto('/admin/module-autodiscovery');
    await expect(page).not.toHaveURL(/\/admin\/module-autodiscovery/, { timeout: 15_000 });
    await expect(page.locator('#mar-name')).toHaveCount(0);
    expect(rulesRequested).toBe(false);
  });
});

test.describe('RBAC — audit user is read-only', () => {
  test.use({ storageState: AUDIT_AUTH });

  test('audit can reach the audit log but not the admin management links', async ({ page }) => {
    await page.goto('/workspaces');
    // Audit-or-admin gate → an audit user sees the Admin▾ menu, and opening it
    // reveals the Audit Log link (its only entry for a non-admin) — #719 IA.
    await page.getByRole('button', { name: 'Admin', exact: true }).click();
    await expect(page.locator('a[href="/admin/audit-log"]')).toBeVisible();
    // …but the admin-only management links are not — they are gated on `admin`
    // and never render for the audit role even with the menu open.
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
