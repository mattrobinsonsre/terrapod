/**
 * The two things a LIVE configure has to get right, neither of which any spec
 * covered before this one.
 *
 * The run page subscribes to `/runs/events` and the configure log stream was
 * wired on both halves -- but nothing asserted that the page ACTS on what
 * arrives, and two defects shipped in that gap:
 *
 * 1. `log_updated` refreshed only the log, never the run. `last-output-at`
 *    lives on the run document (recomputed per read from the progress marker),
 *    so the indicator froze at whatever it said on mount -- and because the
 *    relative time re-renders on every new log chunk, it then counted UPWARD
 *    while output was actively arriving. With `configure_no_progress_seconds`
 *    defaulting to 1800, a healthy run read as about to be killed.
 *
 * 2. `configuring` was absent from the status strip's map, from the `live` set
 *    and from every message catalogue, so it fell through to the documented
 *    raw-status fallback: the lowercase string, a static grey dot, no activity
 *    line and no elapsed timer. A running configure looked inert and finished.
 *
 * Both are asserted here through a mocked SSE body, the same way `log-follow`
 * and `log-tail` prove the plan and apply panes grow. A finite body makes the
 * client reconnect, so the route is hit repeatedly and the handler re-fires.
 */
import { test, expect } from '@playwright/test'

const RUN_ID = '55555555-5555-4555-8555-555555555555'
const WS_ID = '66666666-6666-4666-8666-666666666666'

const STX = '\x02'
const ETX = '\x03'
const HEADER = '#terrapod-host-log v1'
const BODY = [HEADER, `${STX}web1${ETX} ok: [web1]`, `${STX}web1${ETX} changed: [web1]`].join('\n')

/**
 * The run, with a `last-output-at` that MOVES between the first read and every
 * read after it.
 *
 * That is the whole mechanism of the test: the first response is deliberately
 * stale (25 minutes), so a page that never refetches the run is stuck
 * displaying it. Anything that re-reads the run sees a fresh one. Without the
 * `loadRun()` this spec guards, the indicator cannot change.
 */
async function stubRun(page: import('@playwright/test').Page) {
  const reads = { n: 0 }
  await page.route(`**/api/*/runs/${RUN_ID}`, async route => {
    reads.n += 1
    const lastOutput =
      reads.n === 1
        ? new Date(Date.now() - 25 * 60_000).toISOString()
        : new Date(Date.now() - 2_000).toISOString()
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: {
          id: RUN_ID,
          type: 'runs',
          attributes: {
            status: 'configuring',
            message: 'configure-live spec',
            'created-at': '2026-11-02T10:00:00Z',
            'is-configure': true,
            'last-output-at': lastOutput,
            // `configuring-at` is what the strip's `sinceKey` reads for the
            // elapsed timer. It is serialised by the API and was simply never
            // looked up, because the map had no entry to carry the key.
            'status-timestamps': { 'configuring-at': new Date(Date.now() - 90_000).toISOString() },
            actions: {
              'is-confirmable': false,
              'is-discardable': false,
              'is-cancelable': true,
              'is-retryable': false,
            },
          },
          relationships: { workspace: { data: { id: WS_ID, type: 'workspaces' } } },
        },
      }),
    })
  })
  return reads
}

async function stubConfigureLog(page: import('@playwright/test').Page) {
  await page.route(`**/api/terrapod/v1/runs/${RUN_ID}/configure`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: {
          id: RUN_ID,
          type: 'configure',
          attributes: { status: 'running', 'log-read-url': `/stub-logs/${RUN_ID}/configure` },
        },
      }),
    })
  })
  await page.route(`**/stub-logs/${RUN_ID}/configure*`, async route => {
    await route.fulfill({ status: 200, contentType: 'text/plain', body: BODY })
  })
  await page.route(`**/api/*/runs/${RUN_ID}/host-results`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({ data: [], meta: { outstanding: [] } }),
    })
  })
}

/** A `log_updated` for the configure phase, which is what the fix hangs off. */
async function stubConfigureLogEvent(page: import('@playwright/test').Page) {
  await page.route(`**/workspaces/${WS_ID}/runs/events`, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      body: `data: ${JSON.stringify({
        event: 'log_updated',
        run_id: RUN_ID,
        workspace_id: WS_ID,
        phase: 'configure',
      })}\n\n`,
    }))
}

test.describe('a live configure', () => {
  test('refreshes the run when configure output arrives, so the last-output indicator cannot go stale', async ({ page }) => {
    const reads = await stubRun(page)
    await stubConfigureLog(page)
    await stubConfigureLogEvent(page)

    // `?view=configure` because the URL is the source of truth for tab state,
    // and the indicator lives inside the configure log pane rather than on the
    // status strip -- it is rendered only while that pane is streaming.
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    const indicator = page.getByText(/Last output/i).first()
    await expect(indicator).toBeVisible({ timeout: 20_000 })

    // The stale first read is what a page that never refetches is stuck on.
    // Anything that re-reads the run replaces it, so the assertion is that the
    // text LEAVES that value -- not that it reaches a particular one, which
    // would pin the relative-time wording rather than the behaviour.
    await expect(indicator).not.toContainText(/25 minutes/i, { timeout: 20_000 })

    // And prove the refetch is real rather than the indicator having vanished.
    await expect(indicator).toBeVisible()
    expect(reads.n, 'the run must be re-read when configure output arrives').toBeGreaterThan(1)
  })

  test('names the configuring status rather than falling through to the raw string', async ({ page }) => {
    await stubRun(page)
    await stubConfigureLog(page)
    await stubConfigureLogEvent(page)

    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}`)

    // The fallback at the end of the strip's lookup sets `activity: ''`, so an
    // activity line can only be present if the map has a `configuring` entry.
    // Asserting the activity rather than the label is deliberate: the label
    // differs from the raw status only by capitalisation, which is a weaker
    // discriminator than a line that does not exist at all in the fallback.
    await expect(
      page.getByText(/Running the playbooks against the inventory/i).first(),
    ).toBeVisible({ timeout: 20_000 })

    // `configuring` must be in the `live` set too, or there is no elapsed
    // timer: `sinceKey` is read but the 1s ticker never starts.
    await expect(page.getByTitle(/elapsed/i).first()).toBeVisible({ timeout: 20_000 })
  })
})
