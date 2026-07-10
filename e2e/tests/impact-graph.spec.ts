/**
 * Impact graph (#761) — desktop guard.
 *
 * The run page's Impact tab renders a WebGL dependency/blast-radius graph
 * derived from a run's JSON plan output. The E2E stack has no runner pool, so
 * a seeded run never produces plan JSON — which is exactly the gating +
 * graceful-degradation contract this spec pins through the full BFF chain:
 *
 *   - a run WITHOUT plan JSON output does NOT show the Impact tab (gating on
 *     the run's `has-json-output` attribute);
 *   - forcing `?view=impact` mounts the component, which fetches
 *     `/api/terrapod/v1/runs/{id}/impact-graph`, gets a 404, and surfaces the
 *     "not available" banner rather than crashing.
 *
 * The rendered graph itself (WebGL, module clustering, blast-radius highlight)
 * is verified on the live Tilt stack, where a real run produces plan JSON —
 * headless WebGL is not a reliable CI target.
 */
import { test, expect } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api';

test.describe('Impact graph', () => {
  test('tab gates on plan JSON output; component 404s gracefully', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-impact'));
    const runId = await seedRun(token, wsId, true); // queued run, no plan JSON output

    await page.goto(`/workspaces/${wsId}/runs/${runId}`);
    // The run-detail page renders its tab bar.
    await expect(page.getByRole('button', { name: 'Overview' })).toBeVisible({ timeout: 15_000 });
    // No plan JSON output → the Impact tab is not offered.
    await expect(page.getByRole('button', { name: 'Impact', exact: true })).toHaveCount(0);

    // Deep-linking the impact view mounts the component through the real proxy
    // chain; with no plan JSON it fetches the graph, 404s, and shows the banner.
    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=impact`);
    await expect(page.getByText(/Impact graph is not available/i)).toBeVisible({
      timeout: 15_000,
    });
  });
});
