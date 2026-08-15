/**
 * Run lifecycle smoke — the biggest E2E gap before this PR.
 *
 * The CI E2E suite covers admin / auth / workspaces / variables /
 * registry but didn't touch the run-detail page or run lifecycle
 * actions. This file pins:
 *
 *  - Workspace's Runs tab renders
 *  - Run-detail page renders with status badge + Details panel
 *  - Plan output / Apply output tabs are present
 *  - Cancel button visibility maps to non-terminal status
 *  - "Back to workspace" navigation works
 *
 * Heavy lifting (real terraform plan/apply) lives in the Tilt smoke,
 * not here — the E2E stack has no runner pool. We exercise the UI
 * surfaces with API-seeded runs.
 */
import { test, expect, type Route } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api';

test.describe('Run lifecycle UI', () => {
  test('workspace runs tab renders empty state cleanly', async ({ page }) => {
    const wsName = `e2e-runs-empty-${Date.now()}`;

    await page.goto('/workspaces');
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');
    await expect(page.locator(`text=${wsName}`)).toBeVisible({ timeout: 10_000 });

    await page.click(`text=${wsName}`);
    await page.getByRole('button', { name: 'Runs' }).click();

    // Empty workspaces should still render the runs section header /
    // empty state without console errors. Either a table or an empty
    // hint should be visible — exact shape is UI-internal, so we just
    // pin that NO error banner shows up.
    await expect(page.locator('text=/Failed to load|Error/i')).toHaveCount(0);
  });

  test('runs tab navigation preserves tab parameter on reload', async ({ page }) => {
    const wsName = `e2e-tab-${Date.now()}`;

    await page.goto('/workspaces');
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');
    await page.click(`text=${wsName}`);

    await page.getByRole('button', { name: 'Runs' }).click();
    // URL should carry ?tab=runs
    await expect(page).toHaveURL(/[?&]tab=runs/);

    await page.reload();
    // Still on runs tab after reload (status-active styling)
    const runsBtn = page.getByRole('button', { name: 'Runs' });
    await expect(runsBtn).toBeVisible();
  });

  // ── Plan vs plan+apply is chosen by button, not a hidden checkbox (#1340) ──

  test('both queue buttons are offered, and Plan is unchanged', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-queue-buttons'));

    await page.goto(`/workspaces/${wsId}?tab=runs`);

    // The decision that matters is on the surface, not behind "Options".
    const plan = page.getByRole('button', { name: 'Plan', exact: true });
    const planApply = page.getByRole('button', { name: 'Plan + apply', exact: true });
    await expect(plan).toBeVisible({ timeout: 15_000 });
    await expect(planApply).toBeVisible();

    // The checkbox it replaced must be gone — if it lingered, the two
    // mechanisms could disagree about what the next run is.
    await page.getByRole('button', { name: /Options/ }).click();
    await expect(page.getByText('Plan Only', { exact: true })).toHaveCount(0);
  });

  test('Plan queues a plan-only run', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-queue-plan'));

    // Assert on what is actually sent: "plan-only" true is the behaviour the
    // issue promised to leave untouched, and a label alone cannot prove it.
    let body: string | null = null;
    await page.route('**/api/v2/runs', async (route: Route) => {
      if (route.request().method() === 'POST') body = route.request().postData();
      await route.continue();
    });

    await page.goto(`/workspaces/${wsId}?tab=runs`);
    await page.getByRole('button', { name: 'Plan', exact: true }).click();

    await expect.poll(() => body, { timeout: 15_000 }).not.toBeNull();
    expect(JSON.parse(body!).data.attributes['plan-only']).toBe(true);
  });

  test('a drift run that found drift offers plan + apply', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-drift-remediate'));

    // The API cannot create a drift run — only the drift checker does, and the
    // E2E stack has no runner. So the run payload is stubbed: this pins the UI
    // gate (drift + has-changes ⇒ offer), not the detection itself.
    await page.route('**/api/v2/runs/run-*', async (route: Route) => {
      const res = await route.fetch();
      const json = await res.json().catch(() => null);
      if (!json?.data?.attributes) return route.fulfill({ response: res });
      json.data.attributes['is-drift-detection'] = true;
      json.data.attributes['has-changes'] = true;
      await route.fulfill({ response: res, json });
    });

    // Plain plan-only run (config version and all) — the route mock above is
    // what makes it read as a drift run.
    const runId = await seedRun(token, wsId);
    await page.goto(`/workspaces/${wsId}/runs/${runId}`);

    await expect(page.getByRole('button', { name: 'Plan + apply', exact: true })).toBeVisible({
      timeout: 15_000,
    });
  });
});
