/**
 * Log follow engages when an apply starts (#1270).
 *
 * Confirming a planned run switches to the Apply tab BEFORE the run is
 * refetched, so `LogPanel` mounts while the run still reports its pre-confirm
 * status. `following` was seeded with `useState(isStreaming)`, and `useState`
 * reads its argument only on the first render — so it captured `false` and
 * never re-read it once the run became `applying`.
 *
 * Two things this spec has to get right, both learned the hard way:
 *
 *  - **No reload between the two statuses.** A reload remounts the panel
 *    already-streaming, which makes the stale initialiser accidentally
 *    correct — the test would pass on the broken code. The status is moved in
 *    place instead, via an SSE-driven refetch.
 *
 *  - **Assert on scroll position, not the toggle.** `aria-pressed` reads
 *    `true` in BOTH arms; only the actual scroll offset distinguishes them
 *    (measured: 59,528px adrift before the fix, 0px after, 3 runs each side
 *    with no variance). Asserting the toggle alone is a test that cannot fail.
 *
 * The E2E stack has no runner pool, so a seeded run never really applies. The
 * bug is entirely client-side — `isStreaming` is just `status === 'applying'`
 * — so the run's reported status and its apply log are served by interception
 * while everything else goes through the real BFF chain.
 */
import { test, expect } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api';

/** Enough lines that a followed pane is unambiguously scrolled, not near-top. */
const LOG_LINES = 3000;

test.describe('Run log follow', () => {
  test('engages when a confirmed run starts applying', async ({ page }) => {
    // Login, seeding, the confirm round-trip and an SSE reconnect all happen
    // before the assertion — the default budget leaves too little for the
    // retry window, which would surface a real failure as a bare test timeout.
    test.setTimeout(120_000);

    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-follow'));
    const runId = await seedRun(token, wsId, false);
    const bareRunId = runId.replace(/^run-/, '');

    let status = 'planned';

    // Rewrite only the status/actionability on the real run payload.
    await page.route(`**/api/v2/runs/${runId}`, async (route) => {
      const res = await route.fetch();
      const body = await res.json();
      body.data.attributes.status = status;
      body.data.attributes['is-confirmable'] = status === 'planned';
      body.data.attributes['is-discardable'] = status === 'planned';
      await route.fulfill({ response: res, body: JSON.stringify(body) });
    });

    // Swallow the confirm — this is a UI test, not a state mutation. (TFE V2
    // confirms a planned run with `actions/apply`.)
    await page.route(`**/api/v2/runs/${runId}/actions/apply`, async (route) => {
      status = 'confirmed';
      await route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: '{"data":{}}',
      });
    });

    await page.route(`**/api/terrapod/v1/runs/${runId}/apply`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: {
            id: 'apply-1',
            type: 'applies',
            attributes: { 'log-read-url': '/__e2e_follow_log', status: 'running' },
          },
        }),
      }));

    // Serve the log body ONCE. A log that keeps growing keeps moving the
    // scroll geometry, which would make the assertion below racy.
    let logServed = false;
    await page.route('**/__e2e_follow_log*', (route) => {
      const body = logServed
        ? ''
        : Array.from(
            { length: LOG_LINES },
            (_, i) => `apply ${i}  aws_instance.demo: Still creating...`,
          ).join('\n') + '\n';
      logServed = true;
      return route.fulfill({ status: 200, contentType: 'text/plain', body });
    });

    // A finite SSE body makes EventSource reconnect, so the route is hit
    // repeatedly and each hit re-drives loadRun() — which is how the status
    // moves from `confirmed` to `applying` without a reload.
    await page.route(`**/workspaces/${wsId}/runs/events`, (route) =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
        body: `data: ${JSON.stringify({
          event: 'run_status_change',
          run_id: bareRunId,
        })}\n\n`,
      }));

    await page.goto(`/workspaces/${wsId}/runs/${runId}`);

    const confirmBtn = page.getByRole('button', { name: /^confirm|^apply/i }).first();
    await expect(confirmBtn).toBeVisible({ timeout: 20_000 });

    // Confirming is guarded by a native confirm() on every pointer type.
    page.once('dialog', (d) => d.accept());
    await confirmBtn.click();

    // `handleAction` switches to the apply view before it refetches the run,
    // so the panel is now mounted while the run still reports a non-streaming
    // status. That is the precondition for the bug, and the URL is the
    // observable proof it happened. The panel renders an empty state at this
    // point — the apply log is not fetched until the run is `applying` — so
    // there is deliberately no `<pre>` to wait for yet.
    await expect(page).toHaveURL(/[?&]view=apply/, { timeout: 20_000 });

    status = 'applying';

    const pane = page.locator('pre').first();
    await expect(pane).toBeVisible({ timeout: 30_000 });

    // The pane must end up pinned to the tail. `toPass` retries the whole
    // assertion, so this rides out the SSE reconnect interval without a
    // fixed sleep.
    await expect(async () => {
      const distanceFromTail = await pane.evaluate(
        (el) => el.scrollHeight - el.scrollTop - el.clientHeight,
      );
      // Not `=== 0`: sub-pixel line heights leave a fractional remainder.
      expect(distanceFromTail).toBeLessThan(60);
    }).toPass({ timeout: 30_000 });

    // And it says so. (True on the broken code too — kept as a consistency
    // check between what the pane does and what the control claims, never as
    // the discriminating assertion.)
    await expect(page.locator('button[aria-pressed]').first()).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });
});
