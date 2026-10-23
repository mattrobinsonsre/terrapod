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

/**
 * Long enough that the log pane genuinely overflows.
 *
 * Without that, `scrollHeight === clientHeight` and "is it scrolled to the
 * bottom" is true however the component behaves -- the follow assertion below
 * would pass vacuously, which is worse than not having it.
 */
const LONG_TAGGED_BODY = [
  HEADER,
  tag('~', 'PLAY [configure] ***************************************************'),
  ...Array.from({ length: 120 }, (_, i) =>
    tag(i % 2 === 0 ? 'web1' : 'web2', `ok: [${i % 2 === 0 ? 'web1' : 'web2'}] line ${i}`),
  ),
  tag('~', 'PLAY RECAP *********************************************************'),
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

  // `/host-results` is complete from run creation -- it unions the recap rows
  // with the run's resolved inventory -- so `db2` here is a host the run
  // TARGETED and ansible has not reported on. It appears in no tagged log
  // line, which is exactly why the picker cannot be built from the log alone.
  await page.route(`**/api/*/runs/${RUN_ID}/host-results`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: [
          { id: '1', type: 'host-results', attributes: { host: 'web1', status: 'ok' } },
          { id: '2', type: 'host-results', attributes: { host: 'web2', status: 'ignored-failure' } },
          { id: '3', type: 'host-results', attributes: { host: 'db1', status: 'changed' } },
          { id: '4', type: 'host-results', attributes: { host: 'db2', status: 'not-reached' } },
        ],
        meta: { pagination: { 'total-count': 4 } },
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
  test('lists every TARGETED host, including one that has produced no output', async ({ page }) => {
    // The requirement: "a host with no output yet is listed, not hidden -- an
    // inventory host that has not been reached is exactly what someone looks
    // for". `db2` is in no tagged line, so a picker built from the log alone
    // cannot offer it, and the host someone is hunting would be the one host
    // the control omits.
    await stubRun(page, { body: TAGGED_BODY })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    const picker = page.getByTestId('log-hosts-configure')
    await expect(picker).toBeVisible()
    // Log-seen hosts in first-seen order, then the targeted silent one. The
    // host-less marker is not among them.
    await expect
      .poll(async () =>
        picker.locator('option').evaluateAll(opts =>
          opts.map(o => (o as HTMLOptionElement).value),
        ),
      )
      .toEqual(['', 'web1', 'web2', 'db1', 'db2'])
  })

  test('carries each host’s status in the picker as it becomes known', async ({ page }) => {
    // Reusing the Hosts tab's own status strings (#1928), so the two surfaces
    // cannot disagree about a word, and `not-reached` reads as neutral rather
    // than as a failure -- the run did not get to the host.
    await stubRun(page, { body: TAGGED_BODY })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    const picker = page.getByTestId('log-hosts-configure')
    await expect(picker.locator('option', { hasText: /^db2/ })).toContainText('Not reached')
    await expect(picker.locator('option', { hasText: /^web2/ })).toContainText('Failed, ignored')
  })

  test('a targeted host with no output shows the context, not a blank pane', async ({ page }) => {
    // Selecting the host someone came looking for must not read as an error.
    await stubRun(page, { body: TAGGED_BODY })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    const picker = page.getByTestId('log-hosts-configure')
    await expect(picker.locator('option[value="db2"]')).toHaveCount(1)
    await picker.selectOption('db2')

    const pane = paneText(page)
    await expect(pane).toContainText('PLAY RECAP')
    await expect(pane).toContainText('TASK [say hello]')
    await expect(pane).not.toContainText('hello from web1')
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

  test('switching the filter keeps follow pinned to the new tail', async ({ page }) => {
    // #2144 requires the switch to preserve follow. The effect that pins the
    // tail was keyed on the `log` prop, so a filter change -- which alters the
    // rendered text with no new chunk arriving -- did not re-pin: the filtered
    // view opened short of its own tail and stayed there until the next chunk
    // landed, which on a finished run is never.
    await stubRun(page, { body: LONG_TAGGED_BODY, status: 'configuring', complete: false })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=configure`)

    const pane = paneText(page)
    await expect(pane).toContainText('line 119')

    // The fixture must actually overflow, or the assertion below proves nothing.
    const overflows = await pane.evaluate(el => el.scrollHeight > el.clientHeight + 10)
    expect(overflows, 'fixture too short to test scrolling').toBe(true)

    const atBottom = () =>
      pane.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight < 4)

    await expect.poll(atBottom).toBe(true)
    await page.getByTestId('log-hosts-configure').selectOption('web1')
    await expect(pane).not.toContainText('[web2]')
    await expect.poll(atBottom).toBe(true)
  })
})
