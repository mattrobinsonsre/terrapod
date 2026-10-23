import { test, expect } from '@playwright/test'

/**
 * Per-host results for a configure run (#1928).
 *
 * An Ansible configure reports per HOST, so the run page grows a Hosts tab.
 * Three properties here are not visible to any unit test:
 *
 *  - the tab is DATA-GATED, so a terraform/tofu/pulumi run never shows it and
 *    such a deployment pays nothing for Ansible (#1986: no flag);
 *  - a host the run targeted and ansible never reported on is LISTED as
 *    "not reached" rather than omitted — omitting it would show a run that
 *    aborted after one host of four as a clean one-host success;
 *  - both the table and the card list are in the DOM at every width, so every
 *    locator here is scoped with `.filter({ visible: true })`.
 *
 * The phone-width guard lives in `responsive.spec.ts`, which is the only
 * project running a phone viewport -- a copy here would run at desktop width
 * and assert nearly nothing.
 */

const RUN_ID = '33333333-3333-4333-8333-333333333333'
const WS_ID = '44444444-4444-4444-8444-444444444444'

const HOST_RESULTS = {
  data: [
    {
      id: 'rhr-1',
      type: 'run-host-results',
      attributes: {
        host: 'web1',
        status: 'ok',
        ok: 5,
        changed: 0,
        failures: 0,
        unreachable: 0,
        skipped: 1,
        rescued: 0,
        ignored: 0,
        'saw-unreachable': false,
        reached: true,
      },
    },
    {
      id: 'rhr-2',
      type: 'run-host-results',
      attributes: {
        host: 'web2',
        status: 'failed',
        ok: 4,
        changed: 1,
        failures: 1,
        unreachable: 0,
        skipped: 0,
        rescued: 0,
        ignored: 0,
        'saw-unreachable': false,
        reached: true,
      },
    },
    {
      // The counter says zero and the host was in fact unreachable: that is
      // what `saw-unreachable` is for, and the server has already derived the
      // status from it rather than from the count.
      id: 'rhr-3',
      type: 'run-host-results',
      attributes: {
        host: 'hdead',
        status: 'unreachable',
        ok: 2,
        changed: 0,
        failures: 0,
        unreachable: 0,
        skipped: 0,
        rescued: 0,
        ignored: 1,
        'saw-unreachable': true,
        reached: true,
      },
    },
    {
      // Targeted, never reported on. Null counters, not zeros.
      id: 'rhr-4',
      type: 'run-host-results',
      attributes: {
        host: 'db1',
        status: 'not-reached',
        ok: null,
        changed: null,
        failures: null,
        unreachable: null,
        skipped: null,
        rescued: null,
        ignored: null,
        'saw-unreachable': false,
        reached: false,
      },
    },
  ],
  meta: { pagination: { 'current-page': 1, 'page-size': 4, 'total-count': 4, 'total-pages': 1 } },
}

const HOST_EVENTS = {
  data: [
    {
      id: 'rhe-1',
      type: 'run-host-events',
      attributes: {
        host: 'web2',
        kind: 'failed',
        'task-name': 'restart nginx',
        'task-action': 'ansible.builtin.service',
        'task-uuid': 'u-1',
        'play-name': 'configure',
        ignored: false,
        message: 'non-zero return code',
        'payload-ref': null,
        sequence: 7,
      },
    },
  ],
  meta: { pagination: { 'current-page': 1, 'page-size': 1, 'total-count': 1, 'total-pages': 1 } },
}

async function stubRun(
  page: import('@playwright/test').Page,
  { hosts, detail = 'available' }: { hosts: boolean; detail?: string },
) {
  await page.route(`**/api/*/runs/${RUN_ID}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: {
          id: RUN_ID,
          type: 'runs',
          attributes: {
            status: 'applied',
            message: 'host results spec',
            'created-at': '2026-10-23T10:00:00Z',
            'status-timestamps': { 'applied-at': '2026-10-23T10:01:00Z' },
            actions: {
              'is-confirmable': false,
              'is-discardable': false,
              'is-cancelable': false,
              'is-retryable': false,
            },
          },
          relationships: { workspace: { data: { id: WS_ID, type: 'workspaces' } } },
        },
      }),
    })
  })

  // An empty list is what a terraform/tofu/pulumi run returns, and it is what
  // must hide the tab rather than show an empty one.
  await page.route(`**/api/*/runs/${RUN_ID}/host-results`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify(
        hosts
          ? {
              ...HOST_RESULTS,
              meta: { ...HOST_RESULTS.meta, detail: { state: detail } },
            }
          : {
              data: [],
              meta: {
                pagination: {
                  'current-page': 1,
                  'page-size': 0,
                  'total-count': 0,
                  'total-pages': 1,
                },
              },
            },
      ),
    })
  })
  await page.route(`**/api/*/runs/${RUN_ID}/host-events*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify(hosts ? HOST_EVENTS : { data: [], meta: {} }),
    })
  })
}

test.describe('per-host results', () => {
  test('a run with hosts gets a Hosts tab listing every one', async ({ page }) => {
    await stubRun(page, { hosts: true })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=hosts`)

    for (const host of ['web1', 'web2', 'hdead', 'db1']) {
      await expect(
        page.getByText(host, { exact: true }).filter({ visible: true }).first(),
      ).toBeVisible()
    }
  })

  test('a host the run never reached is listed, not hidden', async ({ page }) => {
    await stubRun(page, { hosts: true })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=hosts`)

    // The row exists AND says it was not reached — omitting it would read as a
    // clean three-host run.
    await expect(
      page.getByText('db1', { exact: true }).filter({ visible: true }).first(),
    ).toBeVisible()
    await expect(
      page.getByText('Not reached', { exact: true }).filter({ visible: true }).first(),
    ).toBeVisible()
  })

  test('a dead host reads as unreachable even though its counter is zero', async ({ page }) => {
    await stubRun(page, { hosts: true })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=hosts`)

    await expect(
      page.getByText('Unreachable', { exact: true }).filter({ visible: true }).first(),
    ).toBeVisible()
  })

  test("a host's failures open from its own row", async ({ page }) => {
    await stubRun(page, { hosts: true })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=hosts`)

    await page
      .getByRole('button', { name: /failure/i })
      .filter({ visible: true })
      .first()
      .click()
    await expect(page.getByText('restart nginx').filter({ visible: true }).first()).toBeVisible()
    await expect(
      page.getByText('non-zero return code').filter({ visible: true }).first(),
    ).toBeVisible()
  })

  test('detail removed by retention says so rather than looking complete', async ({
    page,
  }) => {
    // #1928's acceptance: "where retention has expired the detail, the UI SAYS
    // SO rather than rendering an empty panel". The summary rows outlive the
    // JSONL artifact, so without this the panel reads as the whole story.
    await stubRun(page, { hosts: true, detail: 'reaped' })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=hosts`)

    await expect(
      page.getByText(/removed by artifact retention/i).filter({ visible: true }),
    ).toBeVisible()
    // and the summary is still there -- the message explains the detail, it
    // does not replace the panel
    await expect(
      page.getByText('web1', { exact: true }).filter({ visible: true }).first(),
    ).toBeVisible()
    // no download offered for bytes that are gone
    await expect(
      page.getByRole('link', { name: /download full task detail/i }),
    ).toHaveCount(0)
  })

  test('detail still present is reachable, not merely summarised', async ({
    page,
  }) => {
    // The other half of the same clause: "Everything is reachable from the UI.
    // Sluggish is acceptable; absent is not."
    await stubRun(page, { hosts: true, detail: 'available' })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=hosts`)

    const link = page
      .getByRole('link', { name: /download full task detail/i })
      .filter({ visible: true })
    await expect(link).toBeVisible()
    await expect(link).toHaveAttribute(
      'href',
      `/api/v1/runs/${RUN_ID}/host-events/stream`,
    )
    await expect(
      page.getByText(/removed by artifact retention/i),
    ).toHaveCount(0)
  })

  test('a run with no hosts has no Hosts tab at all', async ({ page }) => {
    // The "terraform users pay nothing" property, and the reason there is no
    // flag: the tab is keyed on the data, so a plan or apply never shows one.
    await stubRun(page, { hosts: false })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}`)

    await expect(page.getByRole('button', { name: 'Hosts', exact: true })).toHaveCount(0)
  })

})
