import { test, expect } from '@playwright/test';
import path from 'path';
import { createWorkspace, getStoredToken, seedRun, uniqueName } from '../helpers/api';
import { listOnly, stubLatestRun, NO_ACTIONS } from '../helpers/workspace-list';

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

  // These used to assert the opposite: that the menu OFFERED Plan/Cancel/Apply/
  // Discard to a read-only user and the server refused on press. That was the
  // behaviour, faithfully pinned — and it was the defect (#2579 item 2). The
  // page published `can-queue-*` on every row and read none of them, so the
  // only thing standing between a read-only user and a 403 was the 403.
  //
  // The server is still authoritative and still refuses; what changed is that
  // the control is no longer offered, so the refusal is not something a user
  // has to discover by pressing. Note the item stays FOCUSABLE while disabled,
  // deliberately, so the count beside it is reachable to a screen reader.
  test('bulk Plan and Cancel are not offered without the capability', async ({ page }) => {
    const prefix = uniqueName('e2e-rbac-bulk');
    const token = getStoredToken();
    const wsId = await createWorkspace(token, `${prefix}-a`);
    await seedRun(token, wsId, true);

    await listOnly(page, prefix, 1);
    await page.getByTestId('ws-row-select').check();
    const result = page.getByTestId('ws-bulk-result');

    for (const action of ['plan', 'cancel'] as const) {
      await page.getByTestId('ws-selection').click();
      const item = page.getByTestId(`ws-selection-${action}`);
      await expect(item).toHaveAttribute('aria-disabled', 'true');
      // Zero of one eligible — the row qualifies on run state and the user
      // does not hold the capability, which is the whole distinction.
      await expect(item).toContainText('0 of 1');

      // Pressing it must do nothing at all: no request, so no result panel.
      //
      // `force` is REQUIRED and is the point rather than a smell: Playwright
      // treats `aria-disabled="true"` as not enabled, so a plain `click()`
      // never dispatches and times out after 10s -- proving nothing about the
      // handler. Forcing it past the actionability gate sends a real click, so
      // the assertions below are about the component refusing rather than
      // about Playwright declining to try.
      await item.click({ force: true });
      await expect(result).toHaveCount(0);
      await expect(page.getByTestId('ws-row-select')).toBeChecked();
      await page.keyboard.press('Escape');
    }
  });

  test('bulk Apply and Discard are not offered without the capability', async ({ page }) => {
    const prefix = uniqueName('e2e-rbac-bulk');
    const token = getStoredToken();
    const wsId = await createWorkspace(token, `${prefix}-a`);
    const runId = await seedRun(token, wsId, true);

    // The run is planned and actionable, so run STATE permits both. Only the
    // missing capability withholds them — which is what makes this a test of
    // the permission dimension rather than of eligibility.
    await stubLatestRun(page, [wsId], () => ({
      id: runId,
      status: 'planned',
      actions: { ...NO_ACTIONS, 'is-confirmable': true, 'is-discardable': true },
    }));
    await listOnly(page, prefix, 1);
    await page.getByTestId('ws-row-select').check();
    const result = page.getByTestId('ws-bulk-result');

    for (const action of ['apply', 'discard'] as const) {
      await page.getByTestId('ws-selection').click();
      const item = page.getByTestId(`ws-selection-${action}`);
      await expect(item).toHaveAttribute('aria-disabled', 'true');
      await expect(item).toContainText('0 of 1');

      // No confirm dialog either: a disabled item returns before prompting.
      // `force` for the same reason as above -- without it Playwright never
      // dispatches to an `aria-disabled` item, so nothing is exercised.
      await item.click({ force: true });
      await expect(result).toHaveCount(0);
      await expect(page.getByTestId('ws-row-select')).toBeChecked();
      await page.keyboard.press('Escape');
    }
    await page.unrouteAll({ behavior: 'wait' });
  });
});
