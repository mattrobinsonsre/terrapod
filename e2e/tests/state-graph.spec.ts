/**
 * State resource graph (#765) — desktop guard.
 *
 * The workspace "State Graph" tab renders a WebGL dependency graph of the
 * workspace's Terraform state plus an equivalent accessible table (the #736
 * fallback). WebGL isn't a reliable headless CI target, so this spec drives the
 * deterministic surfaces: the tab loads for a workspace with seeded state, the
 * grouping pivot is present, and the Table view lists the state's resources.
 * The rendered graph itself is a live-Tilt check.
 */
import { test, expect } from '@playwright/test'
import { getStoredToken, createWorkspace, seedStateVersionWithContent, uniqueName } from '../helpers/api'

test.describe('State resource graph', () => {
  test('lists the seeded resources in the accessible table', async ({ page }) => {
    const token = getStoredToken()
    const wsName = uniqueName('e2e-stategraph')
    const wsId = await createWorkspace(token, wsName)

    // A hub + a leaf that depends on it — deterministic 2-node, 1-edge graph.
    await seedStateVersionWithContent(token, wsId, [
      {
        mode: 'managed',
        type: 'null_resource',
        name: 'hub',
        provider: 'provider["registry.terraform.io/hashicorp/null"]',
        instances: [{ schema_version: 0, attributes: {}, dependencies: [] }],
      },
      {
        mode: 'managed',
        type: 'null_resource',
        name: 'leaf',
        provider: 'provider["registry.terraform.io/hashicorp/null"]',
        instances: [{ schema_version: 0, attributes: {}, dependencies: ['null_resource.hub'] }],
      },
    ])

    await page.goto(`/workspaces/${wsId}?tab=state-graph`)

    // The tab's toolbar renders (version + group-by pickers, counts).
    await expect(page.getByText('resources ·', { exact: false })).toBeVisible({ timeout: 15_000 })

    // The graph defaults on desktop; switch to the accessible Table and assert
    // OUR seeded resources are listed (presence, never a global count).
    await page.getByRole('button', { name: 'Table', exact: true }).click()
    await expect(page.getByRole('columnheader', { name: 'Resource' })).toBeVisible()
    await expect(page.getByRole('rowheader', { name: 'null_resource.hub' })).toBeVisible({
      timeout: 10_000,
    })
    await expect(page.getByRole('rowheader', { name: 'null_resource.leaf' })).toBeVisible()

    // The hub is depended on by the leaf → indegree 1 shows in its row.
    const hubRow = page.getByRole('row', { name: /null_resource\.hub/ })
    await expect(hubRow.getByRole('cell', { name: '1', exact: true })).toBeVisible()
  })
})
