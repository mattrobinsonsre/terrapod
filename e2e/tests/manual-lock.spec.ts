import { test, expect } from '@playwright/test';
import path from 'path';
import { createWorkspace, getStoredToken, uniqueName } from '../helpers/api.js';

/**
 * Manual workspace lock (UI) — part of the v0.39.0 locking work. Drives the
 * real lock/unlock control through the browser and asserts the lock actually
 * gates run affordances. The run-execution side of the lock (a locked
 * workspace won't dispatch/confirm an apply) is integration-tested; this
 * confirms the UX surface reflects and drives the lock end-to-end.
 */
const ADMIN_AUTH = path.join(__dirname, '..', '.auth', 'admin.json');

test.describe('Manual workspace lock (UI)', () => {
  test.use({ storageState: ADMIN_AUTH });

  test('lock toggles state and disables the Queue Run/Plan button; unlock restores it', async ({
    page,
  }) => {
    const token = getStoredToken('admin.json');
    const wsId = await createWorkspace(token, uniqueName('e2e-lock'));

    await page.goto(`/workspaces/${wsId}`);

    // Starts unlocked.
    await expect(page.getByText(/unlocked and ready for runs/i)).toBeVisible();
    const queueBtn = page.getByRole('button', { name: /^Queue (Run|Plan)$/ });
    await expect(queueBtn).toBeEnabled();

    // Lock it.
    await page.getByRole('button', { name: 'Lock', exact: true }).click();
    await expect(page.getByText(/this workspace is locked/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Unlock', exact: true })).toBeVisible();

    // The run affordance is gated while locked (manual lock blocks applies).
    await expect(queueBtn).toBeDisabled();

    // Unlock restores both the status and the run affordance.
    await page.getByRole('button', { name: 'Unlock', exact: true }).click();
    await expect(page.getByText(/unlocked and ready for runs/i)).toBeVisible({ timeout: 10_000 });
    await expect(queueBtn).toBeEnabled();
  });
});
