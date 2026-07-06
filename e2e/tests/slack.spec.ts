import { test, expect } from '@playwright/test';
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api';

// Slack integration UI surfaces (#556 / #687).
//   1. The opt-in per-workspace `slack-channel` field round-trips through the
//      workspace settings PATCH and re-renders on reload.
//   2. The /slack/link page is a deliberate confirm flow: it surfaces a clear
//      error for a missing/invalid state and never auto-binds on load.

test.describe('Slack workspace channel', () => {
  test('slack-channel round-trips through workspace settings', async ({ page }) => {
    const token = getStoredToken('admin.json');
    const wsName = uniqueName('e2e-slack-ch');
    const wsId = await createWorkspace(token, wsName);
    const channel = `#e2e-${Date.now()}`;

    await page.goto(`/workspaces/${wsId}`);

    // The opt-in Slack channel input (admin has can-update, so it's editable).
    const input = page.getByPlaceholder(/channel ID/i);
    await expect(input).toBeVisible();
    await expect(input).toHaveValue(''); // starts silent

    await input.fill(channel);
    // Saves on blur → PATCH /api/v2/workspaces/{id}. Wait for the write.
    const [resp] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes(`/api/v2/workspaces/${wsId}`) && r.request().method() === 'PATCH',
      ),
      input.blur(),
    ]);
    expect(resp.ok()).toBeTruthy();

    // Reload: the saved channel must survive server-side (the round-trip is the
    // part that regressed for other settings fields historically).
    await page.reload();
    await expect(page.getByPlaceholder(/channel ID/i)).toHaveValue(channel);
  });
});

test.describe('Slack account link page', () => {
  test('missing state shows an error, does not bind', async ({ page }) => {
    await page.goto('/slack/link');
    await expect(page.getByText(/Missing or invalid link/i)).toBeVisible();
    // No confirm button — nothing to bind.
    await expect(page.getByRole('button', { name: /Confirm/i })).toHaveCount(0);
  });

  test('invalid state is rejected at the confirm/preview step', async ({ page }) => {
    await page.goto('/slack/link?state=not-a-valid-signed-state');
    // The preview call rejects a forged state → error card, never a confirm.
    await expect(page.getByText(/invalid, expired, or already used/i)).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.getByRole('button', { name: /Confirm/i })).toHaveCount(0);
  });
});
