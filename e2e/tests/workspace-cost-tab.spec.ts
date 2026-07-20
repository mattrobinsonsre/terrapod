/**
 * Workspace Cost tab + tab renames (#871).
 *
 * Two things this guards, both through the real BFF proxy chain:
 *
 *  1. The workspace Cost tab renders the current managed-infra cost from the
 *     latest state. A freshly-created workspace has NO state, so the endpoint
 *     returns a zeroed estimate with `state-version: null` WITHOUT needing the
 *     external OpenInfraQuote pricesheet — a deterministic empty state ("No cost
 *     yet…"). The priced table (per-resource names + costs) is verified on the
 *     live Tilt stack, where a real state exists and the pricesheet is cached.
 *
 *  2. The tab rename is REAL, not cosmetic: "Overview" → "Configuration" and
 *     "Configurations" → "Versions", and the query string carries the new keys
 *     (?tab=configuration / ?tab=versions), so a click deep-links correctly.
 */
import { test, expect } from '@playwright/test'
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api'

test.describe('Workspace Cost tab + renames', () => {
  test('Cost tab shows the no-state empty state through the BFF', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2e-wscost'))

    await page.goto(`/workspaces/${wsId}?tab=cost`)
    // Fresh workspace → no state → deterministic empty state (no pricesheet).
    await expect(page.getByText(/No cost yet/i)).toBeVisible({ timeout: 15_000 })
  })

  test('tabs are renamed to Configuration + Versions (+ Cost), URL carries new keys', async ({
    page,
  }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2e-wstabs'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    // Renamed + new tabs present; the old labels are gone.
    await expect(page.getByRole('button', { name: 'Configuration', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Versions', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Cost', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Overview', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Configurations', exact: true })).toHaveCount(0)

    // ?tab=configuration → the workspace settings (was the Overview tab body).
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    // Clicking a renamed tab drives the query string to the NEW key.
    await page.getByRole('button', { name: 'Versions', exact: true }).click()
    await expect(page).toHaveURL(/[?&]tab=versions/)
  })
})
