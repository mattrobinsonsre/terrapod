/**
 * AI architecture critic (#963/#1036) — desktop guard.
 *
 * The run page's Security tab renders the AI architecture critique ON TOP of the
 * deterministic SecurityPanel. Two contracts are pinned here:
 *
 *  1. Gating / self-hide (real BFF, no stubs) — the E2E stack runs with AI
 *     disabled and has no runner pool, so a seeded run never produces a critique
 *     row. The panel must render NOTHING (self-hide on 404) through the full BFF
 *     chain, and — with no security scan either — the Security tab isn't offered
 *     and `?view=security` falls back to Overview. This is the CI-durable guard
 *     (same shape as cost-tab.spec.ts).
 *
 *  2. Placement + SSE-driven refetch (API stubbed) — the critique + security-scan
 *     data path is unreachable in CI (no AI, no runner), so those two GETs and
 *     the run-events SSE stream are stubbed to exercise the component wiring the
 *     absent backend would drive: the critique heading renders ABOVE the
 *     SecurityPanel heading (on top), and an `architecture_critique_ready` SSE
 *     event makes the panel refetch and re-render in place — the exact wiring in
 *     page.tsx (`architectureCritiqueRefresh` bumped in the useRunEvents handler).
 *     The full rendered panel against a real model is verified on the live Tilt
 *     stack.
 */
import { test, expect, type Route } from '@playwright/test'
import { getStoredToken, createWorkspace, seedRun, uniqueName } from '../helpers/api'

test.describe('Architecture critic', () => {
  test('self-hides and Security tab is gated when no critique/scan exists', async ({ page }) => {
    const token = getStoredToken()
    const wsName = uniqueName('e2e-critic')
    const wsId = await createWorkspace(token, wsName)
    const runId = await seedRun(token, wsId, true) // queued plan-only run, no AI, no scan

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=security`)
    // The run-detail page renders (the h1 carries the workspace name).
    await expect(
      page.getByRole('heading', { name: new RegExp(wsName), level: 1 }),
    ).toBeVisible({ timeout: 15_000 })

    // No scan → the Security tab isn't offered and ?view=security falls back to
    // Overview rather than an empty/broken panel.
    await expect(page.getByRole('button', { name: 'Security', exact: true })).toHaveCount(0)
    // The AI critique panel self-hides (404) — its heading never appears.
    await expect(page.getByRole('heading', { name: 'AI architecture review' })).toHaveCount(0)
  })

  test('renders on top of the security panel and refetches on the SSE event', async ({ page }) => {
    const token = getStoredToken()
    const wsName = uniqueName('e2e-critic')
    const wsId = await createWorkspace(token, wsName)
    const runId = await seedRun(token, wsId, true)
    const bareRunId = runId.replace(/^run-/, '')

    // Stub the security scan so the Security tab is offered and SecurityPanel
    // renders (present, passed — not blocked).
    await page.route(/\/security-scan(\?|$)/, (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: {
            id: `security-scan-${bareRunId}`,
            type: 'security-scans',
            attributes: {
              status: 'completed',
              engine: 'checkov',
              findings: [],
              summary: { blocking: 0, critical: 0, high: 0, medium: 0, low: 0 },
              'overridden-by': null,
            },
          },
          meta: { summary: { status: 'passed' } },
        }),
      }),
    )

    // Chat thread messages — empty (the chat self-hides with no messages).
    await page.route(/\/architecture-critique\/messages(\?|$)/, (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({ data: [] }),
      }),
    )

    // The critique GET: serve a "medium risk" v1 on first read, "high risk" v2
    // on every read after the SSE event forces a refetch. Excludes the /messages
    // sub-path so it doesn't shadow the chat route.
    let critiqueReads = 0
    const critiqueBody = (risk: 'medium' | 'high', title: string) => ({
      data: {
        id: `architecture-critique-${bareRunId}`,
        type: 'architecture-critiques',
        attributes: {
          status: 'ready',
          critique: `The proposed architecture is ${risk} risk.`,
          'risk-level': risk,
          findings: [
            {
              severity: risk === 'high' ? 'high' : 'medium',
              category: 'reliability',
              title,
              detail: 'No multi-AZ redundancy for the database tier.',
              address: 'aws_db_instance.main',
            },
          ],
          model: 'claude-e2e',
          'input-tokens': 100,
          'output-tokens': 50,
          'error-message': '',
          language: 'en',
          translated: false,
          'created-at': '2026-01-01T00:00:00Z',
          'updated-at': '2026-01-01T00:00:00Z',
        },
      },
    })
    await page.route(/\/architecture-critique(?!\/messages)(\?|$)/, (route: Route) => {
      critiqueReads += 1
      const v2 = critiqueReads >= 2
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify(
          v2
            ? critiqueBody('high', 'Single point of failure v2')
            : critiqueBody('medium', 'Single point of failure v1'),
        ),
      })
    })

    // Stub the run-events SSE stream to deliver one architecture_critique_ready
    // event — the page's useRunEvents handler bumps architectureCritiqueRefresh,
    // which forces the critique panel to refetch (v1 → v2).
    await page.route(/\/workspaces\/[^/]+\/runs\/events/, (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', 'Content-Encoding': 'none' },
        body: `data: ${JSON.stringify({
          event: 'architecture_critique_ready',
          workspace_id: wsId,
          run_id: bareRunId,
        })}\n\n`,
      }),
    )

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=security`)

    // Both panels render: the AI critique heading and the deterministic scan heading.
    const critiqueHeading = page.getByRole('heading', { name: 'AI architecture review' })
    const scanHeading = page.getByRole('heading', { name: 'Security scan' })
    await expect(critiqueHeading).toBeVisible({ timeout: 15_000 })
    await expect(scanHeading).toBeVisible()

    // Placement contract — the AI critique renders ON TOP of the SecurityPanel.
    const critiqueBox = await critiqueHeading.boundingBox()
    const scanBox = await scanHeading.boundingBox()
    expect(critiqueBox, 'critique heading has a box').not.toBeNull()
    expect(scanBox, 'scan heading has a box').not.toBeNull()
    expect(
      critiqueBox!.y,
      'AI architecture review must render above the Security scan panel',
    ).toBeLessThan(scanBox!.y)

    // A finding renders with its resource address (dir=ltr).
    await expect(page.getByText('aws_db_instance.main')).toBeVisible()

    // The stubbed SSE architecture_critique_ready event forces a refetch — the
    // panel re-renders in place from v1 (medium) to v2 (high) without a reload.
    await expect(page.getByText('Single point of failure v2')).toBeVisible({ timeout: 20_000 })
  })
})
