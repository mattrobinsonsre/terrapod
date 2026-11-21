/**
 * Propagating an autodiscovery rule's update to the workspaces it created
 * (#2621 item 50).
 *
 * The feature was API-only: the admin form saved a rule and could not
 * propagate it. Two tests, because they answer different questions:
 *
 *  1. against the REAL endpoint, that the dry run's payload is accepted and
 *     its report renders -- the half a route mock could never establish,
 *     since `propagate` and `dry_run` are read off the TOP LEVEL of the body
 *     beside `data`, not out of `attributes`;
 *  2. against a MOCKED report, that the verdict table renders every state --
 *     which needs workspaces the rule has actually materialised, and so a
 *     repository to scan.
 */
import { test, expect } from '@playwright/test';
import { API_URL, getStoredToken, uniqueName } from '../helpers/api';

/** A VCS connection, which an autodiscovery rule requires. No network at create time. */
async function createVcsConnection(token: string, name: string): Promise<string> {
  const res = await fetch(`${API_URL}/api/terrapod/v1/vcs-connections`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      data: {
        type: 'vcs-connections',
        attributes: { name, provider: 'gitlab', token: 'glpat-e2e-not-a-real-token' },
      },
    }),
  });
  if (!res.ok) throw new Error(`create vcs connection: ${res.status} ${await res.text()}`);
  return (await res.json()).data.id;
}

async function createRule(token: string, name: string, vcsId: string): Promise<string> {
  const res = await fetch(`${API_URL}/api/terrapod/v1/autodiscovery-rules`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      data: {
        type: 'autodiscovery-rules',
        attributes: {
          name,
          'vcs-connection-id': vcsId,
          'repo-url': 'https://gitlab.com/e2e/propagation-probe',
          pattern: '*',
        },
      },
    }),
  });
  if (!res.ok) throw new Error(`create rule: ${res.status} ${await res.text()}`);
  return (await res.json()).data.id;
}

/** Open the edit form for a named rule, retried for hydration. */
async function openEdit(page: import('@playwright/test').Page, ruleName: string) {
  await page.goto('/admin/autodiscovery');
  const row = page.getByRole('row').filter({ hasText: ruleName });
  await expect(row).toBeVisible({ timeout: 20_000 });
  const propagate = page.getByRole('button', { name: /Save and propagate/ });
  // Retried, and only while the form is still shut, so a click lost to
  // pre-hydration cannot leave the test asserting against a closed form.
  await expect(async () => {
    if ((await propagate.count()) === 0) await row.getByRole('button', { name: 'Edit' }).click();
    await expect(propagate).toBeVisible({ timeout: 1_000 });
  }).toPass({ timeout: 20_000 });
  return propagate;
}

test('the propagate action is offered only for a rule that exists', async ({ page }) => {
  const token = getStoredToken();
  const vcsId = await createVcsConnection(token, uniqueName('e2eprop-vcs'));
  const ruleName = uniqueName('e2eprop-rule');
  await createRule(token, ruleName, vcsId);

  await page.goto('/admin/autodiscovery');
  // The CREATE form cannot propagate: there is no saved rule to propagate, and
  // nothing it has created.
  await page.getByRole('button', { name: 'New Rule' }).click();
  await expect(page.getByRole('button', { name: /Save and propagate/ })).toHaveCount(0);

  // The EDIT form can.
  await openEdit(page, ruleName);
});

test('the dry run reaches the real endpoint and reports an untouched rule', async ({ page }) => {
  const token = getStoredToken();
  const vcsId = await createVcsConnection(token, uniqueName('e2eprop-vcs'));
  const ruleName = uniqueName('e2eprop-rule');
  await createRule(token, ruleName, vcsId);

  const propagate = await openEdit(page, ruleName);
  await propagate.click();

  // This rule has scanned nothing, so it has materialised nothing and the
  // report is empty in all three lists -- which is the state that proves the
  // round trip rather than a rendering of our own fixture.
  await expect(page.getByText(/Nothing would change/)).toBeVisible({ timeout: 20_000 });
  // And with nothing to change, no apply is offered.
  await expect(page.getByRole('button', { name: /^Propagate to/ })).toHaveCount(0);
  await page.getByRole('button', { name: 'Close' }).click();
  await expect(page.getByText(/Nothing would change/)).toHaveCount(0);
});

test('the verdict table distinguishes a change from a value set by hand', async ({ page }) => {
  const token = getStoredToken();
  const vcsId = await createVcsConnection(token, uniqueName('e2eprop-vcs'));
  const ruleName = uniqueName('e2eprop-rule');
  const ruleId = await createRule(token, ruleName, vcsId);

  // A crafted report, because the real one needs workspaces the rule has
  // materialised from a real repository. It carries BOTH diff shapes the
  // service sends: `{from, to}` for a scalar, and a bare name array for a
  // template field whose spec holds secrets.
  await page.route(`**/autodiscovery-rules/${ruleId}`, async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        data: { id: ruleId, type: 'autodiscovery-rules', attributes: { name: ruleName } },
        meta: {
          propagation: {
            dry_run: true,
            matched: 3,
            would_change: [
              {
                id: 'ws-1',
                name: 'payments-prod',
                diff: {
                  'terraform-version': { from: '1.9.0', to: '1.13.1' },
                  'run-tasks': ['nightly-scan'],
                },
              },
            ],
            customised: [
              {
                id: 'ws-2',
                name: 'billing-staging',
                fields: {
                  'terraform-version': { current: '1.11.4', previous: '1.9.0', new: '1.13.1' },
                },
              },
            ],
            unchanged: [{ id: 'ws-3', name: 'search-dev' }],
            skipped_lifecycle: 0,
          },
        },
      }),
    });
  });

  const propagate = await openEdit(page, ruleName);
  await propagate.click();

  // The change: both halves of the scalar diff, and the template field by name.
  await expect(page.getByText('Will change')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('cell', { name: 'payments-prod' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '1.9.0', exact: true })).toBeVisible();
  await expect(page.getByRole('cell', { name: '1.13.1', exact: true }).first()).toBeVisible();
  await expect(page.getByRole('cell', { name: 'nightly-scan' })).toBeVisible();

  // The skip, and its framing. `customised` means the setting is LEFT ALONE --
  // an operator reading this as "your local change will be overwritten" has
  // been told the opposite of what propagation does.
  await expect(page.getByText('Left alone')).toBeVisible();
  await expect(page.getByText(/skips the setting instead of overwriting/)).toBeVisible();
  await expect(page.getByRole('cell', { name: 'billing-staging' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '1.11.4' })).toBeVisible();

  // The already-matching workspace is counted, not listed row by row.
  await expect(page.getByText(/1 workspace already matches/)).toBeVisible();

  // The apply offers the would_change count, not the matched count.
  await expect(page.getByRole('button', { name: 'Propagate to 1 workspace' })).toBeVisible();
});
