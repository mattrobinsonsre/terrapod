/**
 * Workspace Inventory tab (#1967, #1968), through the real BFF proxy chain.
 *
 * The first test is the one that matters most. The tab is DATA-gated, not
 * configuration-gated: there is no Helm flag and no config key to turn it off,
 * because a terraform/tofu-only workspace simply has no inventory rows and so
 * no tab. A regression that made the tab unconditional would be invisible to
 * every other test here — they all seed a host first — so the absence is
 * asserted on its own, against a workspace that has declared nothing.
 *
 * The rest cover what the tab is for: the declared hosts (read-only, because
 * the managing Terraform owns them), the resolution with the freshness that
 * qualifies it, and the limit preview with both of its caveats on screen.
 */
import { test, expect } from '@playwright/test'
import { getStoredToken, createWorkspace, seedInventoryItem, uniqueName } from '../helpers/api'

test.describe('Workspace Inventory tab', () => {
  test('no inventory, no tab — and a stale deep link still renders a page', async ({ page }) => {
    const token = getStoredToken()
    // Deliberately NO seedInventoryItem: this workspace has declared nothing,
    // which is every terraform/tofu-only workspace.
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

  test('declaring a host brings the tab into existence', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvtab'))
    await seedInventoryItem(token, wsId, 'web-1', {
      address: '10.0.0.11',
      groups: ['web'],
      vars: { ansible_user: 'deploy' },
    })

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await expect(page.getByRole('button', { name: 'Inventory', exact: true })).toBeVisible()

    // The declared row, and the statement that Terraform owns it. An operator
    // who is not told this goes looking for an Add button that must not exist.
    await expect(page.getByRole('heading', { name: 'Declared hosts' })).toBeVisible()
    await expect(page.getByText('web-1').first()).toBeVisible()
    await expect(page.getByText(/read-only here/i)).toBeVisible()
    for (const name of ['Add host', 'New host', 'Delete', 'Edit']) {
      await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0)
    }
  })

  test('the resolution says it is a snapshot and when it was taken', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvres'))
    await seedInventoryItem(token, wsId, 'db-1', { address: '10.0.0.21', groups: ['db'] })
    await seedInventoryItem(token, wsId, 'web-1', { address: '10.0.0.11', groups: ['web'] })

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await expect(page.getByRole('heading', { name: 'Resolved hosts and groups' })).toBeVisible()

    // Two declared hosts in two groups. The API resolves a terraform-sourced
    // inventory on first read, so this needs no runner.
    await expect(page.getByText('Hosts', { exact: true })).toBeVisible()
    await expect(page.getByText('Groups', { exact: true })).toBeVisible()

    // The freshness, which is the honest part: a reader sees the LAST snapshot.
    await expect(page.getByText(/Snapshot taken/i)).toBeVisible()
    await expect(page.getByText(/last snapshot, not a live resolution/i)).toBeVisible()
    await expect(page.getByText(/resolved by the API/i)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Refresh', exact: true })).toBeVisible()

    // And which source is why — per-source, not just the inventory's rollup.
    await expect(page.getByRole('heading', { name: 'Sources' })).toBeVisible()
    await expect(page.getByText('terraform', { exact: true })).toBeVisible()
    await expect(page.getByText('API can resolve')).toBeVisible()
  })

  test('the limit preview expands a pattern and shows both caveats', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvlimit'))
    await seedInventoryItem(token, wsId, 'web-1', { groups: ['web'] })
    await seedInventoryItem(token, wsId, 'web-2', { groups: ['web'] })
    await seedInventoryItem(token, wsId, 'db-1', { groups: ['db'] })

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await expect(page.getByRole('heading', { name: 'Resolved hosts and groups' })).toBeVisible()

    // `web:!web-2` — the group minus one host, which is the shape that proves
    // the exclusion operator is really being expanded rather than the group
    // simply being listed.
    await page.getByLabel('Limit pattern').fill('web:!web-2')
    await page.getByRole('button', { name: 'Preview', exact: true }).click()

    await expect(page.getByText(/1 host of 3 selected/i)).toBeVisible()
    const matched = page.locator('li', { hasText: /^web-1$/ })
    await expect(matched.first()).toBeVisible()

    // Both caveats ride WITH the result. A target list read without them is a
    // target list an operator will act on.
    await expect(page.getByText(/Advisory only/i)).toBeVisible()
    await expect(page.getByText(/As fresh as the snapshot/i)).toBeVisible()
  })

  test('a ~regex limit is refused with the reason, not an empty list', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvregex'))
    await seedInventoryItem(token, wsId, 'web-1', { groups: ['web'] })

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await expect(page.getByRole('heading', { name: 'Resolved hosts and groups' })).toBeVisible()

    await page.getByLabel('Limit pattern').fill('~web.*')
    await page.getByRole('button', { name: 'Preview', exact: true }).click()

    // The API refuses a regex term rather than matching nothing, and the UI
    // shows that message: "no hosts match" for a pattern ansible WOULD have
    // expanded is the wrong answer dressed as an answer.
    await expect(page.getByText(/regex/i)).toBeVisible()
    await expect(page.getByText(/host of|hosts of/i)).toHaveCount(0)
  })

  test('the tab says configure operations do not exist yet', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2einvscope'))
    await seedInventoryItem(token, wsId, 'web-1')

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    // This release makes the inventory observable; nothing is run from here.
    // Without this an operator reasonably hunts for a "Run playbook" button.
    await expect(page.getByText(/not available yet/i)).toBeVisible()
    await expect(page.getByRole('button', { name: /playbook|configure/i })).toHaveCount(0)
  })
})
