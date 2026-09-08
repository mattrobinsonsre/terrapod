/**
 * SSE live-update through the full proxy chain (#221).
 *
 * This is the half of the SSE contract that mocked services-tests
 * structurally cannot prove: that an event published server-side travels
 * CDN → ingress → BFF → API → browser EventSource and actually re-renders
 * the page WITHOUT a manual reload. The services-tier test only proves
 * `publish_workspace_event` was called.
 *
 * We open the workspace list, wait for its EventSource to connect, then
 * mutate the org from a *separate* actor (a direct authenticated API call,
 * i.e. not this page's own fetch) and assert the new row appears on the
 * already-loaded page with no `page.reload()`.
 */
import { test, expect } from '@playwright/test';
import { createWorkspace, getStoredToken, uniqueName } from '../helpers/api';

test.describe('SSE live update', () => {
  test('workspace list shows a workspace created by another actor without reload', async ({
    page,
  }) => {
    const token = getStoredToken('admin.json');
    const name = uniqueName('e2e-sse-live');

    // Register the wait for the SSE stream to OPEN before navigating, so we
    // can't miss it: the EventSource opens after hydration (just after load),
    // and pub/sub has no replay — if we created the workspace before the
    // stream was connected, the event would be dropped and the test would
    // flake. Tying creation to "stream is open" makes it deterministic.
    const sseConnected = page.waitForResponse(
      (r) => r.url().includes('/api/terrapod/v1/workspace-events') && r.status() === 200,
      { timeout: 20_000 }
    );

    await page.goto('/workspaces');
    await expect(page.getByRole('heading', { name: 'Workspaces' })).toBeVisible();

    // Not present yet — it doesn't exist.
    await expect(page.locator(`text=${name}`)).toHaveCount(0);

    await sseConnected;

    // Second actor creates the workspace via the API (not this page's fetch).
    await createWorkspace(token, name);

    // The row appears on the already-loaded page, driven purely by the SSE
    // event re-fetching the list. No reload.
    await expect(page.locator(`text=${name}`)).toBeVisible({ timeout: 20_000 });
  });
  test('SSE headers survive the BFF on both API prefixes', async ({ page }) => {
    // #1529 serves the native API at /api/v1 as well as the deprecated
    // /api/terrapod/v1. The BFF sets `Content-Encoding: none` per path
    // (next.config.js); without it the stream is buffered and delivers nothing
    // while still answering 200 — invisible until someone notices the UI has
    // stopped updating.
    //
    // Uses fetch() from the page rather than Playwright's request context: an
    // APIRequestContext call awaits the whole body, and an SSE response never
    // ends, so it would hang to timeout instead of asserting anything. fetch()
    // resolves as soon as headers arrive, and the request is aborted immediately
    // so no stream is left open.
    //
    // The `Content-Encoding: none` header itself is asserted in
    // web/tests/sse-headers.test.ts, not here: a browser strips that header
    // after decoding, so checking it through `fetch` would be unreliable. This
    // spec covers the half the unit test cannot — that the path is actually
    // routed and streams through the real proxy chain on both prefixes.
    await page.goto('/workspaces');

    for (const prefix of ['/api/v1', '/api/terrapod/v1']) {
      const result = await page.evaluate(async (p) => {
        const controller = new AbortController();
        try {
          const res = await fetch(`${p}/workspace-events`, {
            headers: { Accept: 'text/event-stream' },
            signal: controller.signal,
          });
          return {
            status: res.status,
            contentType: res.headers.get('content-type'),
          };
        } finally {
          controller.abort();
        }
      }, prefix);

      expect(result.status, `${prefix}/workspace-events should be served`).toBe(200);
      expect(
        result.contentType,
        `${prefix}/workspace-events should be an event stream`,
      ).toContain('text/event-stream');
    }
  });
});
