/**
 * The run log pane is its final size from first paint while a phase streams (#1547).
 *
 * It used to grow from nothing as output arrived: the empty state rendered no
 * pane at all, and the <pre> was only capped, so every chunk reflowed it until it
 * hit the cap. You could not scroll to where the tail would be until it was
 * already there.
 *
 * The size itself is #2217: the pane is measured against the space actually left
 * below it, not against a fraction of the viewport. `70vh` was wrong in both
 * directions — it ran off the bottom of an ordinary laptop and left a growing
 * band of dead space on a tall monitor — so the assertion here is about the GAP
 * below the pane rather than about a proportion of the window. That also makes
 * it a check on the plumbing no unit test can perform: the height arrives as a
 * CSS custom property set from a measured position, so if that never reaches the
 * stylesheet the gap does not hold.
 *
 * The only two states a streaming pane can be in are "no output yet" and "some
 * output", so the test measures both on one streaming run and requires the same
 * height — no SSE timing, no race. And the other half, so the reservation cannot
 * spread: a finished short log shrinks to fit rather than sitting in a mostly
 * empty reserved box.
 *
 * The E2E stack has no runner pool, so the run's status and its plan log are
 * served by interception; everything else goes through the real BFF chain.
 */
import { test, expect, type Page } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api';

const LOG_URL = '/__e2e_reserve_log';

async function stubRun(page: Page, runId: string, status: string, log: string) {
  await page.route(`**/api/*/runs/${runId}`, async (route) => {
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
    // Reserved rather than collapsed. The magnitude is #2217's and is asserted
    // against the pane's own measured position in the next test; what #1547
    // needs here is that the box exists at its final size with no output in it,
    // which the floor below and the equality further down together state.
    const reservedMin = await page
      .getByTestId('log-reserve-plan')
      .evaluate((el) => getComputedStyle(el).minHeight);
    expect(reservedMin).not.toBe('0px');
    expect(reservedMin).not.toBe('auto');
    expect(empty).toBeGreaterThanOrEqual(parseFloat(reservedMin) - 2);

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
    // Five lines, so well under the reserved size whatever that works out to.
    expect(await paneHeight(page)).toBeLessThan(vh * 0.5);
  });

  test('the gap below the pane does not move when the window does (#2217)', async ({ page }) => {
    test.setTimeout(90_000);
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-reserve-gap'));
    const runId = await seedRun(token, wsId);

    await stubRun(page, runId, 'planning', lines(400));
    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=plan`);
    await expect(page.getByTestId('log-pre-plan')).toBeVisible({ timeout: 20_000 });

    /** Space between the bottom of the pane and the bottom of the window, at
     *  rest. boundingBox() is viewport-relative, so scroll to the top first. */
    async function gapAt(height: number): Promise<number> {
      await page.setViewportSize({ width: 1280, height });
      await page.evaluate(() => window.scrollTo(0, 0));
      let gap = NaN;
      // The height is applied by a layout effect and a ResizeObserver, so let
      // it settle rather than racing the first frame after the resize.
      await expect(async () => {
        const box = (await page.getByTestId('log-pane-plan').boundingBox())!;
        gap = height - (box.y + box.height);
        expect(gap).toBeGreaterThanOrEqual(0);
      }).toPass({ timeout: 10_000 });
      return gap;
    }

    // Derive the two heights from the pane's MEASURED top rather than picking
    // them. A header taller than this spec assumed would put a hardcoded
    // height below the floor, where the pane deliberately stops tracking — and
    // the equality below would then fail for a reason that has nothing to do
    // with the property under test.
    const top = (await page.getByTestId('log-pane-plan').boundingBox())!.y;
    const tall = await gapAt(Math.round(top) + 900);
    const shorter = await gapAt(Math.round(top) + 600);

    // The whole requirement. Note this cannot be satisfied by the pane being
    // stuck at its floor: a fixed height against two window heights 300px apart
    // gives two gaps 300px apart, so equality here IS the tracking property and
    // needs no floor constant of its own. The old 70vh rule gives gaps that
    // differ by 210px, both of them negative — the pane ran off the bottom.
    expect(Math.abs(tall - shorter)).toBeLessThanOrEqual(2);
    // And constant-but-enormous is not a pass either.
    expect(tall).toBeLessThanOrEqual(64);
  });
});
