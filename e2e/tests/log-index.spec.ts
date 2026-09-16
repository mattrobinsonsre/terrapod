import { test, expect } from '@playwright/test'

/**
 * The plan-log index (#1590).
 *
 * A plan log is a long scroll, so the viewer offers a picker: every resource
 * change the plan announces, plus the summary. Choosing one jumps to that line.
 *
 * The index appears only once the server has marked the log complete — the log
 * is framed with STX at the start and ETX at the end, and the follower keeps
 * re-reading a finished phase until ETX arrives (#1591). So these specs serve
 * the log with and without that final byte, which is the difference between
 * "still arriving" and "whole", and is exactly what gates the feature.
 */

const RUN_ID = '11111111-1111-4111-8111-111111111111'
const WS_ID = '22222222-2222-4222-8222-222222222222'

const PLAN_BODY = `Terraform used the selected providers to generate the following execution
plan. Resource actions are indicated with the following symbols:
  + create
  ~ update in-place
  - destroy

Terraform will perform the following actions:

  # aws_instance.web will be updated in-place
  ~ resource "aws_instance" "web" {
        id = "i-0abc"
    }

  # aws_s3_bucket.assets will be created
  + resource "aws_s3_bucket" "assets" {
      + bucket = "assets"
    }

  # aws_iam_role.legacy will be destroyed
  - resource "aws_iam_role" "legacy" {
      - name = "legacy" -> null
    }

Plan: 1 to add, 1 to change, 1 to destroy.
`

const STX = '\x02'
const ETX = '\x03'

/** Serve the run, its plan object, and the log — framed, or still arriving. */
async function stubRun(page: import('@playwright/test').Page, { complete }: { complete: boolean }) {
  await page.route(`**/api/v2/runs/${RUN_ID}`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: {
          id: RUN_ID,
          type: 'runs',
          attributes: {
            status: 'planned',
            message: 'plan-log index spec',
            'created-at': '2026-09-16T10:00:00Z',
            'status-timestamps': { 'planned-at': '2026-09-16T10:01:00Z' },
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

  await page.route(`**/api/terrapod/v1/runs/${RUN_ID}/plan`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/vnd.api+json',
      body: JSON.stringify({
        data: {
          id: 'plan-1',
          type: 'plans',
          attributes: { 'log-read-url': `/stub-logs/${RUN_ID}/plan` },
        },
      }),
    })
  })

  await page.route(`**/stub-logs/${RUN_ID}/plan*`, async route => {
    // STX at offset 0; ETX only when the log is whole.
    const body = `${STX}${PLAN_BODY}${complete ? ETX : ''}`
    await route.fulfill({ status: 200, contentType: 'text/plain', body })
  })
}

test.describe('the plan-log index', () => {
  test('lists every change and the summary once the log is whole', async ({ page }) => {
    await stubRun(page, { complete: true })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=plan`)

    const index = page.getByTestId('log-index-plan')
    await expect(index).toBeVisible()

    // One entry per announced change, in log order, plus the summary.
    const labels = await index.locator('option').allTextContents()
    const entries = labels.slice(1) // the first option is the placeholder
    expect(entries).toHaveLength(4)
    expect(entries[0]).toContain('aws_instance.web')
    expect(entries[1]).toContain('aws_s3_bucket.assets')
    expect(entries[2]).toContain('aws_iam_role.legacy')
    // The summary entry names no address.
    expect(entries[3]).not.toContain('aws_')
  })

  test('jumping to an entry scrolls the viewer to that line', async ({ page }) => {
    await stubRun(page, { complete: true })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=plan`)

    const pane = page.getByTestId('log-pre-plan')
    await expect(pane).toBeVisible()

    const before = await pane.evaluate(el => el.scrollTop)
    // The last announced change is far enough down to require a scroll.
    // Resolve the option's value (its line number) first: selectOption's
    // `label` takes a string, not a pattern, and a pattern is rejected
    // outright rather than simply not matching.
    const index = page.getByTestId('log-index-plan')
    const value = await index
      .locator('option', { hasText: 'aws_iam_role.legacy' })
      .getAttribute('value')
    expect(value).toBeTruthy()
    await index.selectOption(value as string)

    await expect
      .poll(async () => pane.evaluate(el => el.scrollTop), { timeout: 5000 })
      .toBeGreaterThan(before)

    // The chosen line is marked so the eye lands on it.
    await expect(page.locator('[data-log-line].bg-amber-400\\/20')).toHaveCount(1)
  })

  test('does not appear while the log is still arriving', async ({ page }) => {
    // No ETX: the follower keeps reading, so the log is not yet whole and an
    // index built from it would shift its own entries under the operator.
    await stubRun(page, { complete: false })
    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=plan`)

    await expect(page.getByTestId('log-pre-plan')).toBeVisible()
    await expect(page.getByTestId('log-index-plan')).toHaveCount(0)
  })

  test('is a plan-log affordance only', async ({ page }) => {
    await stubRun(page, { complete: true })
    await page.route(`**/api/terrapod/v1/runs/${RUN_ID}/apply`, async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: { id: 'apply-1', type: 'applies', attributes: { 'log-read-url': '' } },
        }),
      })
    })

    await page.goto(`/workspaces/${WS_ID}/runs/${RUN_ID}?view=apply`)
    await expect(page.getByTestId('log-index-apply')).toHaveCount(0)
  })
})
