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

  test('lock then unlock toggles the workspace lock state in the UI', async ({ page }) => {
    const token = getStoredToken('admin.json');
    const wsId = await createWorkspace(token, uniqueName('e2e-lock'));

    await page.goto(`/workspaces/${wsId}`);

    // Starts unlocked: status text + a "Lock" button.
    await expect(page.getByText(/unlocked and ready for runs/i)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Lock', exact: true })).toBeVisible();

    // Lock it → status flips and the button becomes "Unlock".
    await page.getByRole('button', { name: 'Lock', exact: true }).click();
    await expect(page.getByText(/this workspace is locked/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Unlock', exact: true })).toBeVisible();

    // Unlock restores the unlocked state.
    await page.getByRole('button', { name: 'Unlock', exact: true }).click();
    await expect(page.getByText(/unlocked and ready for runs/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Lock', exact: true })).toBeVisible();
  });
});
