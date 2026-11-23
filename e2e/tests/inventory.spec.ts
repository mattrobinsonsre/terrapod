/**
 * Workspace Inventory tab (#1967, #1968, #1969), through the real BFF proxy chain.
 *
 * The first test is the one that matters most. The tab is DATA-gated, not
 * configuration-gated: there is no Helm flag and no config key to turn it off,
 * because a terraform/tofu-only workspace simply holds no hosts, no groups and
 * no declared configure operation and so has no tab. A regression that made the
 * tab unconditional would be invisible to every other test here — they all seed
 * something first — so the absence is asserted on its own.
 *
 * That absence is load-bearing in a way #1966 made sharp: the gate used to read
 * `config-directory != null`, and that column is now non-nullable, so the old
 * test became TRUE for every workspace in the deployment. The gate is
 * `configure-enabled` now, and this is the test that catches it going wrong.
 *
 * The rest cover what the tab is now for. The inventory is seven structures,
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
  seedInventoryHostKey,
  SSH_HOST_KEY_BLOBS,
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

/** As above but DISMISSES, so the guard is shown to abort rather than to ask.
 *  A guard that prompts and then proceeds either way is not a guard, and
 *  accepting every dialog cannot tell the two apart. */
function acceptNextDialogDismissing(page: import('@playwright/test').Page): {
  message: () => string
} {
  let seen = ''
  page.once('dialog', async (d: Dialog) => {
    seen = d.message()
    await d.dismiss()
  })
  return { message: () => seen }
}

test.describe('Workspace Inventory tab', () => {
  test('no inventory, no tab — and a stale deep link still renders a page', async ({ page }) => {
    const token = getStoredToken()
    // Deliberately seeds NOTHING: no host, no group and no declared configure
    // operation, which is every terraform/tofu-only workspace. `plan-apply` is
    // on and `configure` off by default, so this is also what a workspace that
    // predates #1966 reads back as.
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

    // ── the structure is a TREE, and a variable says where it came from ──
    //
    // This replaces three assertions on the old display: `ansible_user=deploy`
    // (a space-joined `k=v` run), and the two separate `Group membership` /
    // `Group nesting` headings. One tree carries all of it, and carries the
    // nesting TRANSITIVELY -- which the old nesting display could not, showing
    // one hop while its own hint promised reach (#2413 F8).
    await expect(page.getByTestId('inv-tree-group-frontend')).toBeVisible()
    await expect(page.getByTestId('inv-tree-group-web')).toBeVisible()
    // `web-1` is two levels down: frontend > web > web-1. Its presence is the
    // transitive claim.
    await expect(page.getByTestId('inv-tree-host-web-1')).toBeVisible()
    await expect(page.getByTestId('inv-tree-host-web-2')).toBeVisible()

    // Nothing is selected yet, so the view asks rather than guessing.
    await expect(page.getByTestId('inv-node-vars')).toHaveCount(0)

    await page.getByTestId('inv-tree-host-web-1').click()

    // YAML, so a value is quoted and cannot be misread -- the whole of the
    // ambiguity the old display had.
    const vars = page.getByTestId('inv-node-vars')
    await expect(vars).toBeVisible()
    await expect(vars).toContainText('ansible_user: "deploy"')
    // AND where it came from, which is why a resolved view exists at all. It
    // was seeded on the host, so nothing in the chain explains it.
    await expect(vars).toContainText('set on this host')

    // The selection is in the URL, so this is a shareable link.
    await expect(page).toHaveURL(/node=web-1/)

    // Selecting a GROUP reports its members rather than inventing a merge:
    // ansible merges group variables into each host, which is what makes a
    // host the unit a resolved value belongs to. `frontend` holds no host
    // directly, so this also proves "no direct hosts" is not read as empty.
    await page.getByTestId('inv-tree-group-frontend').click()
    await expect(page.getByText(/no resolved variables of its own/i)).toBeVisible()
    await expect(page.getByText('no direct hosts')).toBeVisible()
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

  // The inventory settings view is gone (#1929): the roots and defaults are
  // workspace attributes, edited on the Configuration tab, so the test that
  // drove `?inv=settings` was removed with the view rather than rewritten.
})

test.describe('The operations and configure attributes on the workspace Configuration tab', () => {
  /**
   * A workspace declares its operations with TWO BOOLEANS (#1966), and the
   * Ansible roots beside them are WORKSPACE attributes (#1929) — so both are
   * edited here rather than on the Inventory tab, whose settings view is gone.
   *
   * What these assert that a unit test cannot: that the controls actually
   * render, that they round-trip through the real BFF proxy chain, and that the
   * three properties the server enforces are reachable from the UI at all —
   * that `configure-enabled` is what brings the Inventory tab into existence,
   * that an empty config directory reads as the repository root rather than as
   * absent, and that the both-false state is refused before a save is sent.
   *
   * The empty-path case is driven explicitly because it is invisible to any
   * fixture holding a concrete path: a read-only view that rendered `''` as `/`
   * — the way `working-directory` legitimately does, having no declaration to
   * carry — would pass every other test here.
   */
  test('a fresh workspace plans and applies, and does not configure', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2eops'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    // The column defaults, which ARE the compatibility story: every workspace
    // that predates #1966 reads back exactly like this.
    await expect(page.getByTestId('ws-plan-apply-enabled')).toHaveText('Enabled')
    await expect(page.getByTestId('ws-configure-enabled')).toHaveText('Disabled')
  })

  test('enabling configure round-trips, and an empty path reads as the repository root', async ({
    page,
  }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ecfgdir'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Edit', exact: true }).first().click()

    // Turning configure on without typing a path: the config root is the
    // repository root, which is a real value and the common case.
    await page.getByLabel('Configure', { exact: true }).check()
    await page.getByRole('button', { name: 'Save changes', exact: true }).click()

    await expect(page.getByTestId('ws-configure-enabled')).toHaveText('Enabled', {
      timeout: 15_000,
    })
    // `''` says "the repository root" rather than reading as absent. Scoped to
    // the testid rather than page text, because the edit form's own hint
    // carries the same words.
    await expect(page.getByTestId('ws-config-directory')).toHaveText('Repository root')
  })

  test('a config directory, a playbook and a limit round-trip', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ecfgset'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await page.getByRole('button', { name: 'Edit', exact: true }).first().click()

    // A plain text box now — #1966 retired the mode select, because the path
    // stopped carrying the "no configure operation" declaration.
    await page.getByPlaceholder('e.g. ansible').fill('ansible')
    await page.getByPlaceholder('e.g. site.yml').fill('site.yml')
    // `!` is a --limit operator and must survive byte for byte: the server
    // stores these unmodified, because rewriting a submitted value is what
    // makes a Terraform plan disagree with its own result.
    await page.getByPlaceholder('e.g. webservers').fill('web:!web-09')
    await page.getByRole('button', { name: 'Save changes', exact: true }).click()

    await expect(page.getByText('site.yml').filter({ visible: true }).first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText('web:!web-09').filter({ visible: true }).first()).toBeVisible()
  })

  test('turning configure on brings the Inventory tab into existence', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ecfggate'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await expect(page.getByRole('button', { name: 'Inventory', exact: true })).toHaveCount(0)

    await page.getByRole('button', { name: 'Edit', exact: true }).first().click()
    // No path typed, deliberately: the gate is the BOOLEAN, not the path. A
    // gate keyed on `config-directory` being non-empty would fail here, which
    // is the half of #1966 an emptiness test gets wrong.
    await page.getByLabel('Configure', { exact: true }).check()
    await page.getByRole('button', { name: 'Save changes', exact: true }).click()

    // The gate is "hosts, or groups, or a declared configure operation" — this
    // workspace has no rows at all, so the declaration is the whole of its
    // inventory and the tab must appear on its own account.
    await page.reload()
    await expect(page.getByRole('button', { name: 'Inventory', exact: true })).toBeVisible({
      timeout: 15_000,
    })
  })

  test('a workspace cannot be left with no operation at all', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2enoops'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await page.getByRole('button', { name: 'Edit', exact: true }).first().click()

    // Configure is already off, so turning plan/apply off reaches the one pair
    // the server refuses. Save must be blocked BEFORE the request, with the
    // reason beside the controls rather than as a 422 after the fact.
    await page.getByLabel('Plan and apply', { exact: true }).uncheck()
    await expect(page.getByTestId('ws-operations-none')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeDisabled()

    // Turning the other one on clears it: the refusal is about the PAIR, not
    // about either boolean on its own.
    await page.getByLabel('Configure', { exact: true }).check()
    await expect(page.getByTestId('ws-operations-none')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeEnabled()
  })
})

/**
 * Approved SSH host keys (#2592) — which keys Terrapod will trust a host to
 * present, declared here and handed off to OpenSSH as a `known_hosts` file.
 *
 * Three properties carry these tests, and each would be invisible to the
 * others:
 *
 * A host has MANY keys, all of which belong in its trust set — one per
 * algorithm, and two of one algorithm through a rotation — so a surface that
 * modelled it as a field on the host would look right until the second key.
 *
 * The algorithm and the blob are the row's IDENTITY, two thirds of the
 * server's unique constraint, so there is no edit: changing either means a
 * different key, which is a delete and a declare. An Edit control here would
 * promise a rewrite of what a host is trusted to present while keeping the
 * row's id, and that id is the audit trail — so its ABSENCE is asserted.
 *
 * And the two confirm tiers differ on this surface, which is the half a
 * single-tier guard gets wrong. A marker change is reversible and prompts on
 * touch only (the responsive project proves the touch half); a delete cannot
 * be undone and prompts in BOTH modes, which only a precise-pointer project
 * can prove. Hence the delete assertion lives here.
 */
test.describe('Approved host keys', () => {
  test('a key is declared, marked and deleted — and never edited', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ehostkeys'))
    const h1 = await seedInventoryHost(token, wsId, 'web-1')

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=${h1}`)
    await expect(page.getByRole('heading', { name: 'Approved host keys' })).toBeVisible({
      timeout: 15_000,
    })
    // The empty state says what the absence MEANS, rather than reading as an
    // unset field: with no key declared a configure has nothing to check the
    // host against.
    await expect(page.getByText(/No approved keys/)).toBeVisible()

    // Declare one, through the form rather than the API, because the form is
    // what is under test.
    await page.getByRole('button', { name: 'Declare a key', exact: true }).click()
    await page.locator('#inventory-host-key-value').fill(SSH_HOST_KEY_BLOBS['ssh-ed25519'])
    await page.getByRole('button', { name: 'Declare key', exact: true }).click()

    const block = page.getByTestId('inv-host-key')
    await expect(block).toHaveCount(1, { timeout: 15_000 })
    // In FULL. There is nothing to mask — a host's public key is public — and
    // it is the value an operator compares when a host presents something
    // else, so a truncated render would hide the one thing this exists to show.
    await expect(page.getByTestId('inv-host-key-value')).toHaveText(
      SSH_HOST_KEY_BLOBS['ssh-ed25519'],
    )
    // "Approved", not "Trusted": the badge names the state of the DECLARATION.
    // Nothing in the runner reads these rows yet — #2592's own outstanding list
    // names the known_hosts renderer and the ssh handoff — so a badge asserting
    // trust would be claiming an enforcement that does not exist.
    await expect(block.getByText('Approved', { exact: true })).toBeVisible()

    // THE ABSENCE OF AN EDIT. Scoped to the key block, because the host's
    // other sections legitimately have one.
    await expect(block.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(0)
    await expect(page.getByText(/cannot be edited/)).toBeVisible()

    // A host has MANY keys: a second algorithm is an addition, not a
    // replacement.
    await page.getByRole('button', { name: 'Declare a key', exact: true }).click()
    await page.locator('#inventory-host-key-value').fill(SSH_HOST_KEY_BLOBS['ssh-rsa'])
    await page.getByRole('button', { name: 'Declare key', exact: true }).click()
    await expect(page.getByTestId('inv-host-key')).toHaveCount(2, { timeout: 15_000 })

    // Marking is the REVERSIBLE tier, so on a precise pointer it proceeds with
    // no prompt. A dialog here would mean the two tiers had been collapsed
    // into one, which is the drift `useConfirm` exists to prevent.
    let prompted = false
    page.on('dialog', async (d: Dialog) => {
      prompted = true
      await d.accept()
    })
    await page
      .locator('li', { hasText: 'AAAAC3NzaC1lZDI1NTE5' })
      .getByRole('button', { name: 'Mark revoked', exact: true })
      .click()
    await expect(page.getByTestId('inv-host-key-revoked')).toHaveCount(1, { timeout: 10_000 })
    expect(prompted, 'a reversible marker change must not prompt on a precise pointer').toBe(false)

    // Revoked is what the whole feature exists to catch, so it is distinct by
    // its WORD and by its consequence, not by a tint a screen reader cannot
    // report.
    const revoked = page.getByTestId('inv-host-key-revoked')
    await expect(revoked.getByText('Revoked', { exact: true })).toBeVisible()
    await expect(revoked.getByText(/recorded as not to be trusted/)).toBeVisible()

    // And the surface says plainly that Terrapod does not yet verify a
    // connection against any of this. Asserted rather than left to a reader:
    // an operator who declares a key and is not told stops looking, which is
    // the one failure a security surface cannot have.
    await expect(page.getByText(/does not yet use them to verify a connection/)).toBeVisible()

    // Deleting is the IRREVERSIBLE tier: it prompts on a precise pointer too,
    // which is the half the touch suite cannot prove. Dismissed first, so the
    // guard is shown to ABORT rather than merely to ask.
    page.removeAllListeners('dialog')
    const dismissed = acceptNextDialogDismissing(page)
    await revoked.getByRole('button', { name: 'Delete', exact: true }).click()
    await expect.poll(() => dismissed.message(), { timeout: 5_000 }).toContain('ssh-ed25519')
    // And the message points at revoking instead, which is the safe move
    // through a rotation — a delete lets the host present the key again as an
    // unknown one.
    expect(dismissed.message()).toMatch(/revoked instead/)
    await expect(page.getByTestId('inv-host-key-revoked')).toHaveCount(1)

    const accepted = acceptNextDialog(page)
    await revoked.getByRole('button', { name: 'Delete', exact: true }).click()
    await expect.poll(() => accepted.message(), { timeout: 5_000 }).toContain('ssh-ed25519')
    await expect(page.getByTestId('inv-host-key-revoked')).toHaveCount(0, { timeout: 10_000 })
    await expect(page.getByTestId('inv-host-key')).toHaveCount(1)
  })

  test('the server refusals reach the operator as they were written', async ({ page }) => {
    // The API's refusals name the mistake — a whole pasted line, a blob whose
    // embedded algorithm disagrees with the declared type — and each is far
    // more use than "failed to save". So they are surfaced verbatim, and this
    // is what proves the handler is not swallowing them.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ehostkeybad'))
    const h1 = await seedInventoryHost(token, wsId, 'web-1')

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=${h1}`)
    await page.getByRole('button', { name: 'Declare a key', exact: true }).click({ timeout: 15_000 })

    // An ed25519 blob declared as ssh-rsa. The blob itself names its algorithm,
    // so the server can tell — and a key stored under the wrong type is
    // accepted happily by anything that does not look, and then never matches
    // the host.
    await page.getByLabel('Algorithm').selectOption('ssh-rsa')
    await page.locator('#inventory-host-key-value').fill(SSH_HOST_KEY_BLOBS['ssh-ed25519'])
    await page.getByRole('button', { name: 'Declare key', exact: true }).click()
    await expect(page.getByText(/declared as/)).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('inv-host-key')).toHaveCount(0)
  })

  test('the workspace-wide keys read sits BESIDE the resolution', async ({ page }) => {
    // The API serves them as two documents on purpose: the resolved inventory
    // is ansible's own answer, normalised, and the runner renders it back into
    // a YAML inventory where a Terrapod-only section would have no meaning. So
    // this read is an extension beside it, keyed on the host NAME so the two
    // can be read side by side.
    //
    // Deliberately WITHOUT waiting for resolution. The two halves carry
    // separate load and error state, and that is the property under test:
    // resolution can refuse (a source that will not parse, ansible not yet
    // installed from the cache) and the keys are a plain row read that cannot,
    // so an unrelated failure must not hide the trust anchors at the moment
    // they are most worth reading.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ehostkeyover'))
    const h1 = await seedInventoryHost(token, wsId, 'web-1')
    const h2 = await seedInventoryHost(token, wsId, 'web-2')
    await seedInventoryHostKey(token, h1, 'ssh-ed25519')
    await seedInventoryHostKey(token, h1, 'ssh-rsa', { marker: 'revoked' })
    await seedInventoryHostKey(token, h2, 'ssh-ed25519', { marker: 'cert-authority' })

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=resolved`)
    await expect(page.getByRole('heading', { name: 'Approved host keys' })).toBeVisible({
      timeout: 15_000,
    })

    // Three keys over two hosts, one of them revoked. The revoked COUNT is
    // shown whether or not it is zero: a chip that appears only on a bad day
    // teaches a reader to read its absence as "not checked".
    const section = page.locator('section', { hasText: 'Approved host keys' }).first()
    await expect(section.getByText('web-1', { exact: true })).toBeVisible()
    await expect(section.getByText('web-2', { exact: true })).toBeVisible()
    await expect(section.getByTestId('inv-host-key-revoked')).toHaveCount(1)
    await expect(section.getByTestId('inv-host-key')).toHaveCount(2)

    // No editing here, because this document carries no key ids — a second
    // editing surface would be a second place for the two to drift.
    await expect(section.getByRole('button', { name: 'Mark revoked', exact: true })).toHaveCount(0)
    await expect(section.getByRole('button', { name: 'Delete', exact: true })).toHaveCount(0)

    // But a reader who spots the revoked key can get to the host that owns it,
    // which is the only place it can be acted on.
    await section.getByRole('button', { name: 'Open host', exact: true }).first().click()
    await expect(page.getByRole('heading', { name: /Host web-/ })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByRole('heading', { name: 'Approved host keys' })).toBeVisible()
  })
})
