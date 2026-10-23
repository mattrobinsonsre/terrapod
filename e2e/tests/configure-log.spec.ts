import { test, expect } from '@playwright/test'

/**
 * The per-host configure log view (#2144).
 *
 * The requirement is a switch between one host's output and the traditional
 * combined stream, usable MID-RUN in both directions. So these specs serve a
 * tagged log and drive the switch, and one of them serves a log that is still
 * arriving (no ETX) to prove the control does not wait for the run to finish —
 * which is the one thing that separates this from the plan-log index beside it,
 * whose picker deliberately appears only once the log is whole (#1590).
 *
 * The last spec serves an UNTAGGED log, which is what a runner image predating
 * the callback emits. The fallback is the traditional view with the switch
 * absent, keyed on the log's own content. A blank per-host pane would be the
 * recorded "absent is not acceptable" failure.
 */

const RUN_ID = '33333333-3333-4333-8333-333333333333'
const WS_ID = '44444444-4444-4444-8444-444444444444'

const STX = '\x02'
const ETX = '\x03'

const HEADER = '#terrapod-host-log v1'
const tag = (host: string, text: string) => `${host.padEnd(10)} | ${text}`

/**
 * Three hosts, a multi-line result, and a failure on one of them.
 *
 * The continuation lines matter: `ok: [web1] => {` names its host and the
 * `"msg"` and `}` lines after it do not, so a text parser could not attribute
 * them. Keeping them in the fixture is what makes the filter's job real.
 */
const TAGGED_BODY = [
  HEADER,
  tag('~', ''),
  tag('~', 'PLAY [configure] ***************************************************'),
  tag('~', ''),
  tag('~', 'TASK [say hello] ***************************************************'),
  tag('web1', 'ok: [web1] => {'),
  tag('web1', '    "msg": "hello from web1"'),
  tag('web1', '}'),
  tag('web2', 'ok: [web2] => {'),
  tag('web2', '    "msg": "hello from web2"'),
  tag('web2', '}'),
  tag('db1', 'ok: [db1] => {'),
  tag('db1', '    "msg": "hello from db1"'),
  tag('db1', '}'),
  tag('~', ''),
  tag('~', 'TASK [fail on web2] ************************************************'),
  tag('web2', 'fatal: [web2]: FAILED! => {"changed": false, "msg": "deliberate"}'),
  tag('web2', '...ignoring'),
  tag('~', ''),
  tag('~', 'PLAY RECAP *********************************************************'),
  tag('~', 'web1  : ok=1  changed=0  unreachable=0  failed=0'),
  tag('~', 'web2  : ok=2  changed=0  unreachable=0  failed=0  ignored=1'),
  tag('~', 'db1   : ok=1  changed=0  unreachable=0  failed=0'),
].join('\n')

const UNTAGGED_BODY = [
  'PLAY [configure] ***************************************************',
  '',
  'TASK [say hello] ***************************************************',
  'ok: [web1] => {',
  '    "msg": "hello from web1"',
  '}',
  'PLAY RECAP *********************************************************',
].join('\n')

async function stubRun(
  page: import('@playwright/test').Page,
  { body, status = 'applied', complete = true }: { body: string; status?: string; complete?: boolean },
) {
  await page.route(`**/api/*/runs/${RUN_ID}`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: {
          id: RUN_ID,
          type: 'runs',
          attributes: {
            status,
            message: 'configure-log spec',
            'created-at': '2026-10-23T10:00:00Z',
            // What data-gates the Configure tab. Without it the tab is not
            // offered at all, which is the behaviour a terraform run gets.
            'is-configure': true,
            'status-timestamps': {},
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

  await page.route(`**/api/terrapod/v1/runs/${RUN_ID}/configure`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: {
          id: `configure-${RUN_ID}`,
          type: 'configures',
          attributes: { status: 'finished', 'log-read-url': `/stub-logs/${RUN_ID}/configure` },
        },
      }),
    })
  })

  await page.route(`**/stub-logs/${RUN_ID}/configure*`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'text/plain',
      body: `${STX}${body}\n${complete ? ETX : ''}`,
    })
  })
}

/** The log text as rendered, whichever of the three render branches is active. */
function paneText(page: import('@playwright/test').Page) {
  return page.getByTestId('log-pre-configure').filter({ visible: true }).first()
}

test.describe('the configure log', () => {
  test('offers a host picker listing every host that produced output', async ({ page }) => {
    await stubRun(page, { body: TAGGED_BODY })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    const picker = page.getByTestId('log-hosts-configure')
    await expect(picker).toBeVisible()
    // First-seen order, and the host-less marker is not among them.
    const values = await picker.locator('option').evaluateAll(opts =>
      opts.map(o => (o as HTMLOptionElement).value),
    )
    expect(values).toEqual(['', 'web1', 'web2', 'db1'])
  })

  test('shows ansible’s own words with no tag in the combined view', async ({ page }) => {
    await stubRun(page, { body: TAGGED_BODY })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    const pane = paneText(page)
    await expect(pane).toContainText('ok: [web1]')
    // The prefix is the viewer's framing, not ansible's, so the traditional
    // view must not show it.
    await expect(pane).not.toContainText('web1       |')
  })

  test('filters to one host and back again, mid-run, with the log still arriving', async ({ page }) => {
    // `configuring` plus no ETX: the run is live and the log is incomplete.
    // This is the requirement the plan-log index does NOT meet — its picker
    // waits for the log to be whole — so serving an unfinished log here is the
    // point of the spec rather than incidental.
    await stubRun(page, { body: TAGGED_BODY, status: 'configuring', complete: false })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    const picker = page.getByTestId('log-hosts-configure')
    await expect(picker).toBeVisible()

    const pane = paneText(page)
    await expect(pane).toContainText('hello from web2')

    await picker.selectOption('web1')
    await expect(pane).toContainText('hello from web1')
    await expect(pane).not.toContainText('hello from web2')
    await expect(pane).not.toContainText('hello from db1')

    // Back to the combined stream — the switch has to work in both directions.
    await picker.selectOption('')
    await expect(pane).toContainText('hello from web2')
    await expect(pane).toContainText('hello from db1')
  })

  test('keeps the task banners and the recap when one host is selected', async ({ page }) => {
    // Context, not noise: a result with no banner above it is a line with
    // nothing to attach it to.
    await stubRun(page, { body: TAGGED_BODY })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    await page.getByTestId('log-hosts-configure').selectOption('web1')
    const pane = paneText(page)
    await expect(pane).toContainText('TASK [say hello]')
    await expect(pane).toContainText('PLAY RECAP')
    await expect(pane).toContainText('TASK [fail on web2]')
  })

  test('attributes the continuation lines of a multi-line result', async ({ page }) => {
    // The whole argument for a callback over a parser: these lines name no
    // host in ansible's own output.
    await stubRun(page, { body: TAGGED_BODY })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    await page.getByTestId('log-hosts-configure').selectOption('web2')
    const pane = paneText(page)
    await expect(pane).toContainText('"msg": "hello from web2"')
    await expect(pane).not.toContainText('"msg": "hello from web1"')
    // `...ignoring` carries no host in the text form either.
    await expect(pane).toContainText('...ignoring')
  })

  test('falls back to the traditional view when the runner did not tag the log', async ({ page }) => {
    // An older runner image. The switch is simply not offered; the log still
    // renders in full. Showing an empty per-host pane instead would be worse
    // than showing no switch.
    await stubRun(page, { body: UNTAGGED_BODY })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    await expect(paneText(page)).toContainText('ok: [web1]')
    await expect(page.getByTestId('log-hosts-configure')).toHaveCount(0)
  })

  test('offers no Configure tab on a run that has no configure', async ({ page }) => {
    // Data-gated, not flagged (#1986): a terraform or pulumi run has no hosts
    // and no configure, so it pays nothing for Ansible being present.
    await page.route(`**/api/*/runs/${RUN_ID}`, async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: {
            id: RUN_ID,
            type: 'runs',
            attributes: {
              status: 'applied',
              message: 'no configure here',
              'created-at': '2026-10-23T10:00:00Z',
              'is-configure': false,
              'status-timestamps': {},
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
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}`)
    await expect(page.getByTestId('log-pre-configure')).toHaveCount(0)
  })
})
