/**
 * What the run document says a run IS, through the real BFF proxy chain
 * (#2507, #2509).
 *
 * Two defects, one cause. A **configure-only** run was labelled "plan + apply"
 * *and* "configure" in the workspace run list — as if it were a composite —
 * because the cell derived the kind from `is-configure`, `configure-after-apply`
 * and the workspace's `plan-apply-enabled`, and had no case for a run with no
 * plan/apply half at all. And opening that run landed on Overview, when the
 * configure log is the only log it has.
 *
 * Both now read one derived attribute, `run-kind`. What a unit test cannot
 * settle is whether the SERVER actually emits it and the PAGE actually reads
 * it: `web/tests/run-kind-attribute.test.ts` drives the two helpers and
 * `services/tests/services/test_run_kind.py` drives the producer, and between
 * them a page that still infers the kind would pass both. This is the hop that
 * joins them.
 *
 * The playbook a configure ran is asserted here too (#2527), for the same
 * reason: on a workspace whose runs are mostly configures, the kind badge alone
 * does not tell two of them apart.
 */
import { test, expect } from '@playwright/test'
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api'

const API_URL = process.env.API_URL || 'http://localhost:8000'

/** Queue a standalone configure through the API, returning its run id. */
async function seedConfigure(
  token: string,
  workspaceId: string,
  playbook?: string,
): Promise<string> {
  // A run needs an uploaded configuration version first -- `create_run`
  // refuses one without it, which is what every configure seeded here was
  // hitting. Same shape as `seedRun` in e2e/helpers/api.ts, on the native
  // surface because a configure is not a TFE concept.
  const cvRes = await fetch(
    `${API_URL}/api/v1/workspaces/${workspaceId}/configuration-versions`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.api+json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        data: { type: 'configuration-versions', attributes: { 'auto-queue-runs': false } },
      }),
    },
  )
  if (!cvRes.ok) throw new Error(`seedConfigure cv: ${cvRes.status} ${await cvRes.text()}`)
  const cvJson = await cvRes.json()
  // No Authorization header, and FOLLOW the returned `upload-url` rather than
  // rebuilding it from the id -- rebuilding produces the bare guessable address
  // the signed capability replaced.
  const upRes = await fetch(cvJson.data.attributes['upload-url'] as string, {
    method: 'PUT',
    body: 'terrapod-e2e-config-placeholder',
  })
  if (!upRes.ok) throw new Error(`seedConfigure upload: ${upRes.status}`)

  const attributes: Record<string, unknown> = { 'is-configure': true, 'plan-only': false }
  if (playbook) attributes.playbook = playbook
  const res = await fetch(`${API_URL}/api/v1/runs`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      data: {
        type: 'runs',
        attributes,
        relationships: { workspace: { data: { type: 'workspaces', id: workspaceId } } },
      },
    }),
  })
  if (!res.ok) throw new Error(`seedConfigure: ${res.status} ${await res.text()}`)
  return (await res.json()).data.id
}

test.describe('The run document says what kind of run it is', () => {
  test('a configure-only run is badged configure alone, not "plan + apply" too', async ({
    page,
  }) => {
    const token = getStoredToken()
    // Configure-only, which is the shape the defect was reported on. It also
    // takes the plan+apply QUEUE BUTTON off the page, which matters: that
    // button's label is the same string as the badge, so on a workspace
    // offering both operations an unscoped negative would match the button
    // and fail for a reason that has nothing to do with the run list.
    const wsId = await createWorkspace(token, uniqueName('e2erunkind'), {
      'plan-apply-enabled': false,
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'release.yml',
    })
    await seedConfigure(token, wsId)

    await page.goto(`/workspaces/${wsId}?tab=runs`)

    // Scoped to the run row's own type cell. The page keeps BOTH renders in
    // the DOM at every width -- a desktop table and a phone card list -- so an
    // unscoped locator matches the hidden one too.
    const typeCell = page.locator('td').filter({ hasText: 'configure' }).filter({ visible: true })
    await expect(typeCell.first()).toBeVisible()

    // The defect, asserted as an ABSENCE: this run has no plan and no apply,
    // so the "Plan + apply" badge beside the configure one was saying that
    // Terraform was changing infrastructure. Scoped to the cell AND to what is
    // visible, so neither the phone render nor any other surface can satisfy
    // or break it.
    await expect(
      typeCell.getByText('Plan + apply', { exact: true }).filter({ visible: true }),
    ).toHaveCount(0)
  })

  test('a plan run still gets its plan + apply badge', async ({ page }) => {
    // The other direction, and it is what makes the absence above mean
    // something: a cell that had simply stopped rendering the default badge
    // would pass that test and break this one.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erunkindpa'))
    // Through the shared helper, which uploads a configuration version first:
    // a run on a non-VCS workspace needs one, and building it by hand here
    // would be a second copy of that sequence.
    await seedRun(token, wsId, false)

    await page.goto(`/workspaces/${wsId}?tab=runs`)
    // Scoped to the run row, like the negative above and for a sharper reason
    // here: this workspace DOES offer plan+apply, so its queue button carries
    // the identical label. An unscoped locator would be satisfied by the
    // button and would pass with the badge deleted.
    await expect(
      page
        .locator('td')
        .getByText('Plan + apply', { exact: true })
        .filter({ visible: true })
        .first(),
    ).toBeVisible()
  })

  test('the run list shows which playbook a configure used', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erunkindpb'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'release.yml',
      playbooks: ['release.yml', 'restart.yml'],
    })
    await seedConfigure(token, wsId, 'restart.yml')

    await page.goto(`/workspaces/${wsId}?tab=runs`)

    // The run named its own, so that is what the row shows -- not the
    // workspace's default, which is the value it would show had the run named
    // nothing. Scoped to the row: the same filename is an `<option>` in the
    // run-options select on this page, and an option stays in the DOM.
    await expect(
      page
        .locator('td')
        .getByText('restart.yml', { exact: true })
        .filter({ visible: true })
        .first(),
    ).toBeVisible()
  })

  test('a configure-only run opens on its Configure tab', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erunkindtab'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'release.yml',
    })
    const runId = await seedConfigure(token, wsId)

    // No `?view=`, which is the whole case: the page has to choose.
    await page.goto(`/workspaces/${wsId}/runs/${runId}`)

    // The Configure pane, not the Overview. Asserted on the pane rather than
    // on the tab's highlight, because the highlight is a class and the pane is
    // the thing the reader came for.
    await expect(page).toHaveURL(/view=configure/)
  })

  test('an explicit ?view= still wins', async ({ page }) => {
    // The constraint the issue is explicit about: a deep link keeps working,
    // so the default can never take a tab away from somebody who asked for one.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erunkinddeep'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'release.yml',
    })
    const runId = await seedConfigure(token, wsId)

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=details`)

    await expect(page).toHaveURL(/view=details/)
    await expect(page).not.toHaveURL(/view=configure/)
  })
})
