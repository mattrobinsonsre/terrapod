import { test, expect } from '@playwright/test';
import path from 'path';
import { createWorkspace, getStoredToken, lockWorkspace, uniqueName } from '../helpers/api.js';

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
    // The holder is reported while the lock is held (#1705). A UI lock gives
    // no reason, so none is shown.
    await expect(page.getByTestId('lock-holder')).toContainText('Locked by');
    await expect(page.getByTestId('lock-reason')).toHaveCount(0);

    // Unlock restores the unlocked state.
    await page.getByRole('button', { name: 'Unlock', exact: true }).click();
    await expect(page.getByText(/unlocked and ready for runs/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Lock', exact: true })).toBeVisible();
    await expect(page.getByTestId('lock-holder')).toHaveCount(0);
  });

  test('a lock taken with a reason shows why and by whom, until it is released (#1705)', async ({ page }) => {
    const token = getStoredToken('admin.json');
    const wsId = await createWorkspace(token, uniqueName('e2e-lock-reason'));
    const reason = `maintenance window ${uniqueName('note')}`;
    await lockWorkspace(token, wsId, reason);

    await page.goto(`/workspaces/${wsId}`);

    await expect(page.getByText(/this workspace is locked/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('lock-reason')).toHaveText(`Reason: ${reason}`);
    await expect(page.getByTestId('lock-holder')).toContainText('Locked by');

    await page.getByRole('button', { name: 'Unlock', exact: true }).click();
    await expect(page.getByText(/unlocked and ready for runs/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('lock-reason')).toHaveCount(0);
    await expect(page.getByTestId('lock-holder')).toHaveCount(0);
  });
});
