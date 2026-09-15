/**
 * A finished plan log shows its tail without a reload (#1591).
 *
 * The runner uploads the stored log, which carries the tail (the `Plan: …`
 * summary and the entrypoint's closing lines), from its exit handler, AFTER
 * the run has turned terminal. Until then the log endpoint serves the live
 * snapshot without the end-of-log marker (ETX). The page used to stop polling
 * at the transition and make one fetch, which landed in that gap, so the tail
 * never appeared until the page was refreshed.
 *
 * This spec reproduces the gap exactly: while the run streams, the log is a
 * snapshot; after it turns `planned`, the first read still returns the
 * snapshot with no ETX, and only later reads return the full log with ETX.
 * The page must get there on its own, with no reload, and show every line
 * exactly once.
 *
 * The E2E stack has no runner pool, so the run's status and its plan log are
 * served by interception; everything else goes through the real BFF chain.
 */
import { test, expect } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api';

const LOG_URL = '/__e2e_tail_log';
const STX = '\x02';
const ETX = '\x03';
const SNAPSHOT =
  'Initializing the backend...\n' +
  'null_resource.demo: Refreshing state... [id=1234]\n';
const SUMMARY = 'Plan: 1 to add, 0 to change, 0 to destroy.';
const FULL = SNAPSHOT + `${SUMMARY}\nPLAN_HAS_CHANGES=true\n`;

test.describe('Run log tail', () => {
  test('the plan summary appears after the run finishes, with no reload', async ({ page }) => {
    test.setTimeout(120_000);

    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-tail'));
    const runId = await seedRun(token, wsId);
    const bareRunId = runId.replace(/^run-/, '');

    let status = 'planning';
    // Reads at offset 0 after the phase ended — the first is in the gap.
    let finishedReads = 0;

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

    await page.route(`**${LOG_URL}*`, (route) => {
      const offset = new URL(route.request().url()).searchParams.get('offset');
      let body = '';
      if (offset === '0') {
        if (status === 'planning') {
          body = STX + SNAPSHOT;
        } else {
          finishedReads += 1;
          body = finishedReads === 1 ? STX + SNAPSHOT : STX + FULL + ETX;
        }
      }
      return route.fulfill({ status: 200, contentType: 'text/plain', body });
    });

    // A finite SSE body makes EventSource reconnect, so each reconnect
    // re-drives loadRun() — which is how the status moves without a reload.
    await page.route(`**/workspaces/${wsId}/runs/events`, (route) =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
        body: `data: ${JSON.stringify({ event: 'run_status_change', run_id: bareRunId })}\n\n`,
      }));

    let loads = 0;
    page.on('load', () => {
      loads += 1;
    });

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=plan`);
    const pre = page.getByTestId('log-pre-plan');
    await expect(pre).toContainText('Refreshing state', { timeout: 20_000 });
    await expect(pre).not.toContainText(SUMMARY);

    // The run finishes; the stored log has not been uploaded yet.
    status = 'planned';

    await expect(pre).toContainText(SUMMARY, { timeout: 45_000 });
    await expect(pre).toContainText('PLAN_HAS_CHANGES=true');
    // The gap was actually exercised: at least one tail-less read after the
    // transition, then the full one.
    expect(finishedReads).toBeGreaterThanOrEqual(2);

    // Every line exactly once: nothing duplicated across the snapshot/full join.
    const text = (await pre.textContent()) ?? '';
    expect(text.split('Refreshing state').length - 1).toBe(1);
    expect(text.split(SUMMARY).length - 1).toBe(1);
    expect(text).not.toContain(ETX);

    expect(loads).toBe(1);
  });
});
