/**
 * The run log pane is its final size from first paint while a phase streams (#1547).
 *
 * It used to grow from nothing as output arrived: the empty state rendered no
 * pane at all, and the <pre> was only CAPPED at 70vh, so every chunk reflowed it
 * until it hit the cap. You could not scroll to where the tail would be until it
 * was already there.
 *
 * The only two states a streaming pane can be in are "no output yet" and "some
 * output", so the test measures both on one streaming run and requires the same
 * height — no SSE timing, no race. And the other half, so the reservation cannot
 * spread: a finished short log shrinks to fit rather than sitting in a 70vh box.
 *
 * The E2E stack has no runner pool, so the run's status and its plan log are
 * served by interception; everything else goes through the real BFF chain.
 */
import { test, expect, type Page } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api';

const LOG_URL = '/__e2e_reserve_log';

async function stubRun(page: Page, runId: string, status: string, log: string) {
  await page.route(`**/api/v2/runs/${runId}`, async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    body.data.attributes.status = status;
    await route.fulfill({ response: res, body: JSON.stringify(body) });
  });
  await page.route(`**/api/terrapod/v1/runs/${runId}/plan`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: { id: 'plan-1', type: 'plans', attributes: { 'log-read-url': LOG_URL, status: 'running' } },
      }),
    }));
  // Serve the whole log at offset 0 and nothing after, so the content is fixed
  // while it is measured.
  await page.route(`**${LOG_URL}*`, (route) => {
    const first = new URL(route.request().url()).searchParams.get('offset') === '0';
    return route.fulfill({ status: 200, contentType: 'text/plain', body: first ? log : '' });
  });
}

async function paneHeight(page: Page): Promise<number> {
  const pane = page.getByTestId('log-pane-plan');
  await expect(pane).toBeVisible({ timeout: 20_000 });
  return (await pane.boundingBox())!.height;
}

function lines(n: number): string {
  return Array.from({ length: n }, (_, i) => `plan ${i}  data.external.wait: Still reading...`).join('\n') + '\n';
}

test.describe('Run log pane height', () => {
  test('a streaming pane is full height before output and stays that size', async ({ page }) => {
    test.setTimeout(90_000);
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-reserve'));
    const runId = await seedRun(token, wsId);
    const vh = page.viewportSize()!.height;

    // Streaming, no output yet: the pane must already be at its reserved size.
    await stubRun(page, runId, 'planning', '');
    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=plan`);
    const empty = await paneHeight(page);
    expect(empty).toBeGreaterThanOrEqual(vh * 0.7);

    // Streaming, output arrived: the same height. A short log too — the
    // reservation, not the content, sets the size while the phase runs.
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await stubRun(page, runId, 'planning', lines(12));
    await page.reload();
    await expect(page.getByTestId('log-pre-plan')).toBeVisible({ timeout: 20_000 });
    const withOutput = await paneHeight(page);
    expect(Math.abs(withOutput - empty)).toBeLessThanOrEqual(2);
  });

  test('a finished short log shrinks to fit', async ({ page }) => {
    test.setTimeout(90_000);
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-reserve-done'));
    const runId = await seedRun(token, wsId);
    const vh = page.viewportSize()!.height;

    await stubRun(page, runId, 'planned', lines(5));
    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=plan`);
    await expect(page.getByTestId('log-pre-plan')).toBeVisible({ timeout: 20_000 });
    expect(await paneHeight(page)).toBeLessThan(vh * 0.5);
  });
});
