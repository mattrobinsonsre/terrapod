/**
 * Workspace Inventory tab (#1967, #1968, #1969), through the real BFF proxy chain.
 *
 * The first test is the one that matters most. The tab is DATA-gated, not
 * configuration-gated: there is no Helm flag and no config key to turn it off,
 * because a terraform/tofu-only workspace simply holds no hosts, no groups and
 * no settings and so has no tab. A regression that made the tab unconditional
 * would be invisible to every other test here — they all seed something first —
 * so the absence is asserted on its own.
 *
 * The rest cover what the tab is now for. The inventory is eight structures,
 * each its own addressable resource, so the premise is PER-ROW EDITING: one
 * host, one group, one membership, one nesting, one variable at a time. The
 * earlier read-only panel is gone, and so is its "declared by Terraform, so
 * read-only here" copy — which is asserted absent, because that sentence
 * surviving would send an operator looking for a button that is now right in
 * front of them.
 *
 * Two properties of the resolved view are asserted because getting either wrong
 * misleads: a group's host list is DIRECT membership only (ansible does not
 * flatten nesting into it, so a parent whose hosts all arrive through a child
 * reports none of its own), and `?limit=` DOES expand through nesting — and now
 * expands a `~regex` rather than refusing it, which inverts what this file used
 * to assert.
 */
import { test, expect, type Dialog } from '@playwright/test'
import {
  getStoredToken,
  createWorkspace,
  seedInventoryHost,
  seedInventoryGroup,
  seedInventoryMembership,
  seedInventoryNesting,
  seedInventoryVar,
  waitForInventoryResolution,
  uniqueName,
} from '../helpers/api'

/** Accept the next confirm() and capture its text. Registered BEFORE the click:
 *  window.confirm() is synchronous and blocks the handler, so waiting for the
 *  event and then clicking deadlocks. */
function acceptNextDialog(page: import('@playwright/test').Page): { message: () => string } {
  let seen = ''
  page.once('dialog', async (d: Dialog) => {
    seen = d.message()
    await d.accept()
  })
  return { message: () => seen }
}

test.describe('Workspace Inventory tab', () => {
  test('no inventory, no tab — and a stale deep link still renders a page', async ({ page }) => {
    const token = getStoredToken()
    // Deliberately seeds NOTHING: no host, no group, no settings, which is
    // every terraform/tofu-only workspace.
    const wsId = await createWorkspace(token, uniqueName('e2einvgate'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await expect(page.getByRole('button', { name: 'Configuration', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Inventory', exact: true })).toHaveCount(0)

    // A link someone kept from a workspace that DID have one must not land on a
    // blank pane — it falls back to Configuration, the same way a Terraform-only
    // tab does on a Pulumi workspace.
    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Inventory', exact: true })).toHaveCount(0)
  })

  test('one host brings the tab into existence, and its row is editable', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvtab'))
    await seedInventoryHost(token, wsId, 'web-1')

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await expect(page.getByTestId('inventory-tab')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Inventory', exact: true })).toBeVisible()

    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible()
    await expect(page.getByText('web-1').filter({ visible: true }).first()).toBeVisible()

    // The row is editable now, which is the whole change. Both halves asserted:
    // the actions exist…
    await expect(page.getByRole('button', { name: 'Edit', exact: true }).first()).toBeVisible()
    await expect(page.getByRole('button', { name: 'Delete', exact: true }).first()).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add host', exact: true })).toBeVisible()
    // …and the copy that used to tell the operator they do not is gone. A revert
    // of the prose alone would pass every other assertion here.
    await expect(page.getByText(/read-only here/i)).toHaveCount(0)
    await expect(page.getByText(/Declared by this workspace/i)).toHaveCount(0)
  })

  test('the sub-view lives in the URL, so it survives a reload', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvurl'))
    await seedInventoryGroup(token, wsId, 'web')

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await page.getByRole('button', { name: 'Groups', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Groups', exact: true })).toBeVisible()
    await expect(page).toHaveURL(/inv=groups/)

    // Reload, not re-navigate: a view held only in component state would come
    // back as Hosts and the operator would lose their place on every refresh.
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Groups', exact: true })).toBeVisible()
    await expect(page.getByText('web').filter({ visible: true }).first()).toBeVisible()
  })

  test('a host is added, renamed and deleted through the UI', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvcrud'))
    // One seeded row so the tab exists at all; everything else is driven here.
    await seedInventoryHost(token, wsId, 'db-1')

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible()

    await page.getByLabel('New host name').fill('web-9')
    await page.getByRole('button', { name: 'Add host', exact: true }).click()
    await expect(page.getByText('web-9').filter({ visible: true }).first()).toBeVisible()

    // Drill in by the host's own name, then rename it. The detail view is where
    // a host's variables and memberships live, so it is also the rename surface.
    await page.getByRole('button', { name: 'web-9', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Host web-9' })).toBeVisible()
    await page.getByLabel('Host name').fill('web-10')
    await page.getByRole('button', { name: 'Rename', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Host web-10' })).toBeVisible()

    // Back to the list and delete it. Tier 1 — an irreversible delete prompts a
    // confirm() on a precise pointer too, and the prompt names the host.
    await page.getByRole('button', { name: 'Back to hosts' }).click()
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible()
    const row = page.locator('tr', { hasText: 'web-10' })
    const dialog = acceptNextDialog(page)
    await row.getByRole('button', { name: 'Delete', exact: true }).click()
    await expect.poll(() => dialog.message(), { timeout: 5_000 }).toContain('web-10')
    await expect(page.getByText('web-10').filter({ visible: true })).toHaveCount(0, {
      timeout: 10_000,
    })
    // The host that was not deleted is still there — a delete that cleared the
    // list would pass the assertion above.
    await expect(page.getByText('db-1').filter({ visible: true }).first()).toBeVisible()
  })

  test('a host detail edits its variables and its group memberships', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvhost'))
    const hostId = await seedInventoryHost(token, wsId, 'web-1')
    await seedInventoryGroup(token, wsId, 'web')
    await seedInventoryVar(token, { host: hostId }, 'ansible_user', 'deploy')

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=${hostId}`)
    await expect(page.getByRole('heading', { name: 'Host web-1' })).toBeVisible()

    // The seeded variable, and one added here.
    await expect(page.getByText('ansible_user').filter({ visible: true }).first()).toBeVisible()
    await page.getByRole('button', { name: 'Add variable', exact: true }).click()
    await page.getByLabel('Key', { exact: true }).fill('ansible_port')
    await page.getByLabel('Value', { exact: true }).fill('2222')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText('ansible_port').filter({ visible: true }).first()).toBeVisible()

    // The membership is a LINK, so adding one leaves both ends alone and the
    // chip that appears addresses the link rather than either end.
    await expect(page.getByText(/in no group/i)).toBeVisible()
    await page.getByLabel('Choose a group').selectOption({ label: 'web' })
    await page.getByRole('button', { name: 'Add to group', exact: true }).click()
    await expect(page.getByRole('button', { name: /Remove this host from web/ })).toBeVisible({
      timeout: 10_000,
    })

    // Removing it is tier 2 — reversible, so a precise pointer proceeds with no
    // dialog. A spy that trips would mean the wrong tier was applied.
    let prompted = false
    const spy = async (d: Dialog) => {
      prompted = true
      await d.dismiss()
    }
    page.on('dialog', spy)
    await page.getByRole('button', { name: /Remove this host from web/ }).click()
    await expect(page.getByText(/in no group/i)).toBeVisible({ timeout: 10_000 })
    expect(prompted).toBe(false)
    page.off('dialog', spy)
  })

  test('a group detail nests another group, and the child sees its parent', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvnest'))
    const parent = await seedInventoryGroup(token, wsId, 'frontend')
    const child = await seedInventoryGroup(token, wsId, 'web')
    const hostId = await seedInventoryHost(token, wsId, 'web-1')
    await seedInventoryMembership(token, child, hostId)

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=${parent}`)
    await expect(page.getByRole('heading', { name: 'Group frontend' })).toBeVisible()

    // The parent holds no host of its own — only the nested group does — which
    // is exactly the case the resolved view has to render honestly.
    await expect(page.getByText(/No hosts in this group directly/i)).toBeVisible()
    await page.getByLabel('Choose a group to nest').selectOption({ label: 'web' })
    await page.getByRole('button', { name: 'Add child group', exact: true }).click()
    await expect(page.getByRole('button', { name: /Remove web from the children/ })).toBeVisible({
      timeout: 10_000,
    })

    // The same link from the child's side: one row, two ends, both addressable.
    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=${child}`)
    await expect(page.getByRole('heading', { name: 'Group web' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Parent groups' })).toBeVisible()
    await expect(page.getByRole('button', { name: /Remove this group from frontend/ })).toBeVisible(
      { timeout: 10_000 },
    )
  })

  test('inventory variables are group_vars/all, and a sensitive one reads back masked', async ({
    page,
  }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvvars'))
    await seedInventoryHost(token, wsId, 'web-1')
    await seedInventoryVar(token, { workspace: wsId }, 'ansible_user', 'deploy')
    await seedInventoryVar(token, { workspace: wsId }, 'become_pass', 'hunter2', {
      sensitive: true,
    })

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=vars`)
    await expect(page.getByRole('heading', { name: 'Inventory variables' })).toBeVisible()
    await expect(page.getByText(/group_vars\/all/)).toBeVisible()

    await expect(page.getByText('ansible_user').filter({ visible: true }).first()).toBeVisible()
    await expect(page.getByText('deploy').filter({ visible: true }).first()).toBeVisible()

    // The mask, and — the part that matters — the stored value nowhere on the
    // page. `sensitive` is a display flag over a column that is always
    // encrypted, so the API returns the mask and never the secret.
    await expect(page.getByText('become_pass').filter({ visible: true }).first()).toBeVisible()
    await expect(page.getByText('***').filter({ visible: true }).first()).toBeVisible()
    await expect(page.getByText('hunter2')).toHaveCount(0)
  })

  test('the resolved view is live, shows direct membership only, and names the nesting', async ({
    page,
  }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvres'))
    const parent = await seedInventoryGroup(token, wsId, 'frontend')
    const web = await seedInventoryGroup(token, wsId, 'web')
    const h1 = await seedInventoryHost(token, wsId, 'web-1')
    const h2 = await seedInventoryHost(token, wsId, 'web-2')
    await seedInventoryMembership(token, web, h1)
    await seedInventoryMembership(token, web, h2)
    await seedInventoryNesting(token, parent, web)
    await seedInventoryVar(token, { host: h1 }, 'ansible_user', 'deploy')
    // Ansible does the resolution, and the API installs ansible-core on first
    // use, so this both warms it and turns "no route to the PyPI cache" into a
    // named failure instead of a mystery assertion timeout.
    await waitForInventoryResolution(token, wsId)

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=resolved`)
    await expect(page.getByRole('heading', { name: 'Resolved hosts' })).toBeVisible()

    // The COUNTS, not the labels. A stat chip renders its label and value
    // adjacently, so an anchored regex matches that chip alone — and asserting
    // a label exists would prove nothing about whether anything resolved.
    // `getByText('Groups', { exact: true })` is also a strict-mode violation
    // here, across the chip, the nav button and the table headers.
    await expect(page.getByText(/^Hosts\s*2$/)).toBeVisible({ timeout: 30_000 })
    // `all`, `ungrouped`, `web` and `frontend` — ansible derives the first two,
    // which is the reason the merge is its job and not ours.
    await expect(page.getByText(/^Groups\s*[1-9]/)).toBeVisible()

    // The honest part: the read resolved these rows to answer itself, so the
    // panel says it is live and offers nothing to refresh.
    await expect(page.getByText(/This is live/i)).toBeVisible()
    await expect(page.getByRole('button', { name: /refresh/i })).toHaveCount(0)
    await expect(page.getByText(/This resolution dates from/i)).toHaveCount(0)

    // A host's resolved variables, after the merge.
    await expect(page.getByText(/ansible_user=deploy/).first()).toBeVisible()

    // Direct membership only, said in as many words — and the nesting rendered
    // beside it, so `frontend` reporting no hosts of its own cannot be read as
    // "frontend is empty".
    await expect(page.getByRole('heading', { name: 'Group membership' })).toBeVisible()
    await expect(page.getByText(/Direct membership only/i)).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Group nesting' })).toBeVisible()
    await expect(page.getByText('frontend').filter({ visible: true }).first()).toBeVisible()
  })

  test('a limit expands through nesting, and a ~regex is expanded rather than refused', async ({
    page,
  }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvlimit'))
    const parent = await seedInventoryGroup(token, wsId, 'frontend')
    const web = await seedInventoryGroup(token, wsId, 'web')
    const h1 = await seedInventoryHost(token, wsId, 'web-1')
    const h2 = await seedInventoryHost(token, wsId, 'web-2')
    const db = await seedInventoryHost(token, wsId, 'db-1')
    const dbg = await seedInventoryGroup(token, wsId, 'db')
    await seedInventoryMembership(token, web, h1)
    await seedInventoryMembership(token, web, h2)
    await seedInventoryMembership(token, dbg, db)
    await seedInventoryNesting(token, parent, web)
    await waitForInventoryResolution(token, wsId)

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=resolved`)
    await expect(page.getByText(/^Hosts\s*3$/)).toBeVisible({ timeout: 30_000 })

    // `frontend` holds no host directly — only the nested `web` does — so a
    // limit naming it proves the expansion walks the nesting rather than
    // reading the group's own member list.
    await page.getByLabel('Limit pattern').fill('frontend')
    await page.getByRole('button', { name: 'Apply limit', exact: true }).click()
    await expect(page.getByText(/^Hosts\s*2$/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/Limited to frontend/i)).toBeVisible()

    // The exclusion operator, which proves the pattern is being expanded rather
    // than the group simply being listed.
    await page.getByLabel('Limit pattern').fill('web:!web-2')
    await page.getByRole('button', { name: 'Apply limit', exact: true }).click()
    await expect(page.getByText(/^Hosts\s*1$/)).toBeVisible({ timeout: 30_000 })

    // A `~regex` term is EXPANDED now, not refused: ansible performs the
    // expansion, so its own pattern language works in full. This file used to
    // assert the opposite, which is why the old refusal copy is asserted absent.
    await page.getByLabel('Limit pattern').fill('~web.*')
    await page.getByRole('button', { name: 'Apply limit', exact: true }).click()
    await expect(page.getByText(/^Hosts\s*2$/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/regex.*not supported|regex.*refused/i)).toHaveCount(0)

    // Clearing goes back to the whole inventory, so an operator cannot be left
    // reading a limited answer as the full one.
    await page.getByRole('button', { name: 'Clear', exact: true }).click()
    await expect(page.getByText(/^Hosts\s*3$/)).toBeVisible({ timeout: 30_000 })
  })

  test('settings are absent by default, and that is said rather than shown as an error', async ({
    page,
  }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvset'))
    await seedInventoryHost(token, wsId, 'web-1')

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=settings`)
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()

    // A 404 from the settings read is the ORDINARY state — the row exists only
    // to bind a repository or to switch the declared rows off — so it must read
    // as a default, never as a failure.
    await expect(page.getByText(/which is the default/i)).toBeVisible()
    await expect(page.getByText(/Failed to/i)).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Add settings', exact: true })).toBeVisible()

    // Opening the form offers the repository binding and defaults to none.
    await page.getByRole('button', { name: 'Add settings', exact: true }).click()
    await expect(page.getByLabel('VCS connection')).toBeVisible()
    await expect(page.getByLabel('Repository')).toBeVisible()
    await expect(page.getByLabel('Ignored paths')).toBeVisible()
  })
})
