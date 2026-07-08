import { test, expect, type Dialog } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRunTask, uniqueName } from '../helpers/api';

/**
 * #719 two-tier confirm() policy — the DESKTOP (precise-pointer) half.
 *
 * The maintainer does not test on a real touch device, so these Playwright
 * assertions are the contract that proves the guards behave and catch
 * regressions. This file runs on Desktop Chrome (fine pointer); the coarse-
 * pointer half lives in responsive.spec.ts (Pixel).
 *
 * Tier 1 — irreversible delete/remove → confirm() in BOTH modes (so it fires
 *          even on a precise pointer, asserted here).
 * Tier 2 — other mutation (toggle) → confirm() on touch ONLY (so on a precise
 *          pointer it proceeds with NO dialog, asserted here).
 *
 * A run task exposes both a Delete (tier 1) and an Enable/Disable toggle
 * (tier 2) on one unambiguous row, so it exercises the whole matrix.
 */
test.describe('confirm() guards — precise pointer', () => {
  test('delete prompts a confirm; reversible toggle does not', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('confirm-desktop'));
    const rtName = uniqueName('rt');
    await seedRunTask(token, wsId, rtName);

    await page.goto(`/workspaces/${wsId}?tab=run-tasks`);
    await expect(page.getByText(rtName)).toBeVisible({ timeout: 15_000 });

    // Tier 2 — toggle must NOT prompt on a precise pointer; it proceeds and the
    // status pill flips to "Disabled". Register a dialog spy that would trip if
    // an unexpected confirm() appeared.
    let toggleDialogFired = false;
    const spy = async (d: Dialog) => { toggleDialogFired = true; await d.dismiss(); };
    page.on('dialog', spy);
    await page.getByRole('button', { name: 'Disable' }).click();
    await expect(page.getByText('Disabled', { exact: true })).toBeVisible({ timeout: 10_000 });
    expect(toggleDialogFired).toBe(false);
    page.off('dialog', spy);

    // Tier 1 — delete MUST prompt a confirm() even on a precise pointer.
    // Register the handler BEFORE the click: window.confirm() is synchronous and
    // blocks the click handler, so it must be accepted as it opens (waitForEvent
    // + click deadlocks).
    let deleteMsg = '';
    page.once('dialog', async (d) => { deleteMsg = d.message(); await d.accept(); });
    await page.getByRole('button', { name: 'Delete' }).click();
    await expect.poll(() => deleteMsg, { timeout: 5_000 }).toContain('Delete run task');
    await expect(page.getByText(rtName)).toBeHidden({ timeout: 10_000 });
  });
});
