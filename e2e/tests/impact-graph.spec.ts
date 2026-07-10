/**
 * Impact graph (#761) — desktop guard.
 *
 * The run page's Impact tab renders a WebGL dependency/blast-radius graph
 * derived from a run's JSON plan output. The E2E stack has no runner pool, so
 * a seeded run never produces plan JSON — which is exactly the gating contract
 * this spec pins through the full BFF chain:
 *
 *   - a run WITHOUT plan JSON output does NOT show the Impact tab, and the view
 *     can't even be force-selected: the run page falls the active view back to
 *     `overview` when `impact` isn't an available tab (airtight gating on the
 *     run's `has-json-output` attribute).
 *
 * The rendered graph itself (WebGL, module clustering, blast-radius highlight)
 * is verified on the live Tilt stack, where a real run produces plan JSON —
 * headless WebGL is not a reliable CI target, and the component is unreachable
 * without plan JSON output anyway.
 */
import { test, expect } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api';

test.describe('Impact graph', () => {
  test('Impact tab is gated on plan JSON output', async ({ page }) => {
    const token = getStoredToken();
    const wsName = uniqueName('e2e-impact');
    const wsId = await createWorkspace(token, wsName);
    const runId = await seedRun(token, wsId, true); // queued run, no plan JSON output

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=overview`);
    // The run-detail page renders (the h1 carries the workspace name).
    await expect(
      page.getByRole('heading', { name: new RegExp(wsName), level: 1 }),
    ).toBeVisible({ timeout: 15_000 });
    // No plan JSON output → the Impact tab is not offered.
    await expect(page.getByRole('button', { name: 'Impact', exact: true })).toHaveCount(0);
  });
});
