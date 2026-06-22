import { test, expect } from '@playwright/test';
import path from 'path';
import { createWorkspace, getStoredToken, uniqueName } from '../helpers/api.js';

/**
 * SSE live-update — proves the workspace-list SSE event re-renders the page
 * through the full BFF proxy chain WITHOUT a manual reload. This is the half
 * of the SSE contract that a services-api test (asserting `publish_event` was
 * called) cannot prove: that the browser actually received the event over the
 * proxied stream and re-fetched.
 */
const ADMIN_AUTH = path.join(__dirname, '..', '.auth', 'admin.json');

test.describe('SSE live updates', () => {
  test.use({ storageState: ADMIN_AUTH });

  test('workspace list picks up a newly-created workspace without reload', async ({ page }) => {
    const token = getStoredToken('admin.json');
    await page.goto('/workspaces');
    // Wait until the list page is interactive.
    await expect(page.getByRole('heading', { name: /workspaces/i }).first()).toBeVisible();

    // Create a workspace out-of-band (different origin than this page's render).
    const name = uniqueName('e2e-sse');
    await createWorkspace(token, name);

    // It should appear via the workspace_list_events SSE stream — no reload.
    await expect(page.getByText(name, { exact: false })).toBeVisible({ timeout: 15_000 });
  });
});
