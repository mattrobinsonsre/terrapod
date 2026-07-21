/**
 * Cost tab (#871) — desktop guard.
 *
 * The run page's Cost tab renders the runner-produced cost estimate (a native
 * OpenInfraQuote-port estimate of the plan's monthly cost delta). The E2E stack
 * has no runner pool, so a seeded run never produces a cost estimate — which is
 * exactly the gating contract this spec pins through the full BFF chain:
 *
 *   - a run WITHOUT a cost estimate does NOT show the Cost tab, and the view
 *     can't be force-selected: the run page falls the active view back to
 *     `overview` when `cost` isn't an available tab (airtight gating on the
 *     run's `has-cost-estimate` attribute).
 *
 * The rendered panel itself (headline cards, per-resource table on desktop /
 * stacked cards on mobile, unpriced bucket, OpenInfraQuote credit) is verified
 * on the live Tilt stack, where a real run produces a cost estimate — the panel
 * is unreachable in CI without one, the same as the Impact tab.
 *
 * The AI cost estimate section (#871 — the model pricing what oiq couldn't,
 * plus savings advisories and a narrative) lives on this same tab, below the
 * deterministic panel, and self-hides (renders nothing) when no cost-summary
 * row exists (404). It is behind the SAME gate: no runner in CI means no cost
 * estimate, so no Cost tab, so the AI section can't render either — and the
 * E2E stack runs with AI disabled regardless. Like CostPanel, its rendered
 * output (AI-estimated-resource table, advisory badges, demoted narrative,
 * "never added to the priced total" provenance line) is verified on the live
 * Tilt stack against a real agent run, not in CI.
 */
import { test, expect } from '@playwright/test'
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api'

test.describe('Cost tab', () => {
  test('Cost tab is gated on a cost estimate', async ({ page }) => {
    const token = getStoredToken()
    const wsName = uniqueName('e2e-cost')
    const wsId = await createWorkspace(token, wsName)
    const runId = await seedRun(token, wsId, true) // queued run, no cost estimate

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=cost`)
    // The run-detail page renders (the h1 carries the workspace name).
    await expect(
      page.getByRole('heading', { name: new RegExp(wsName), level: 1 }),
    ).toBeVisible({ timeout: 15_000 })
    // No cost estimate → the Cost tab is not offered, and ?view=cost falls back
    // to Overview rather than showing an empty/broken Cost panel.
    await expect(page.getByRole('button', { name: 'Cost', exact: true })).toHaveCount(0)
  })
})
