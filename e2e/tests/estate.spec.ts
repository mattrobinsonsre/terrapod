/**
 * Estate topology (#763) — desktop guard.
 *
 * The /estate page renders a WebGL graph of the estate plus an equivalent
 * accessible table (the #736 fallback). WebGL itself isn't a reliable headless
 * CI target, so this spec drives the deterministic surfaces: the page renders,
 * the group-by pivot is built from the estate's real label keys, and the Table
 * view lists the caller's workspaces. The rendered graph is a live-Tilt check.
 */
import { test, expect } from '@playwright/test'
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api'

test.describe('Estate topology', () => {
  test('renders, pivots by a real label key, and the table lists workspaces', async ({ page }) => {
    const token = getStoredToken()
    const wsName = uniqueName('e2e-estate')
    // A label guarantees a "group by label: team" axis exists in the pivot.
    await createWorkspace(token, wsName, { labels: { team: 'estate-e2e' } })

    await page.goto('/estate')
    await expect(page.getByRole('heading', { name: 'Estate topology', level: 1 })).toBeVisible({
      timeout: 15_000,
    })

    // The group-by axes are derived from the DATA (no baked-in convention):
    // our team label must show up as an option.
    const groupBy = page.locator('select')
    await expect(groupBy).toBeVisible()
    await expect(groupBy.locator('option', { hasText: 'label: team' })).toHaveCount(1)

    // The accessible Table view lists workspaces — assert OUR workspace is there
    // (never a global count; the estate is shared across concurrent specs).
    await page.getByRole('button', { name: 'Table', exact: true }).click()
    await expect(page.getByRole('columnheader', { name: 'Workspace' })).toBeVisible()
    await expect(page.getByRole('rowheader', { name: wsName })).toBeVisible({ timeout: 10_000 })
  })
})
