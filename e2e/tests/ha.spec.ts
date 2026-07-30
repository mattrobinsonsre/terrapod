import { test, expect, type Page, type Route } from '@playwright/test';
import path from 'path';
import { expectNoHorizontalPageScroll } from '../helpers/responsive';

/**
 * The high-availability admin page (#1163).
 *
 * Driven against mocked `/ha/*` responses rather than the live stack. That is
 * deliberate and is the only way this suite earns its keep: the e2e stack is a
 * healthy single node, so every honesty property this page exists for — a
 * follower banner, a backfilling class, a sampled blob check, an irreplaceable
 * class nobody verified — is unreachable without a fixture. Testing it against
 * the real single-node stack would assert only that the happy path renders,
 * which is precisely the case that cannot mislead anyone.
 *
 * The properties under test are the three the page must never get wrong:
 *   1. backfilling ⇒ NOT in sync, however fresh the last cycle was;
 *   2. a SAMPLED blob check is never presented as a verdict on the estate;
 *   3. an unchecked irreplaceable class is never presented as a pass.
 */

const USER_AUTH = path.join(__dirname, '..', '.auth', 'user.json');

const BASE_STATUS = {
  'node-id': 'node-a',
  role: 'leader',
  'peer-configured': true,
  'replication-enabled': true,
  'last-sync-at': '2026-01-01T00:00:00Z',
  'seconds-since-last-sync': 4,
  'backfilling-classes': [] as string[],
  'in-sync': true,
  'events-retained': 128,
  'oldest-event-age-seconds': 3600,
  'retention-seconds': 604800,
  'replicated-classes': ['workspaces'],
  components: [
    { name: 'api', ready: 2, desired: 2, nodes: 2, zones: 2, pdb: true, 'pdb-permits-disruption': true },
  ],
  'schedulable-nodes': 3,
  'cluster-zones': 3,
  'ha-findings': [] as unknown[],
  'components-sampled-at': '2026-01-01T00:00:00Z',
  'components-unavailable-reason': null,
  'single-replica-components': [] as string[],
};

const BASE_READINESS = {
  sampled: true,
  'missing-total': 0,
  'irreplaceable-missing': [] as string[],
  'irreplaceable-unchecked': [] as string[],
  'duration-ms': 12,
  'unavailable-reason': null,
  classes: [
    {
      name: 'state',
      tier: 'irreplaceable',
      mode: 'copy',
      verifiable: true,
      note: '',
      'total-rows': 400,
      checked: 20,
      missing: 0,
      'missing-examples': [] as string[],
      complete: false,
      error: null,
    },
  ],
};

/** Serve `/ha/status` (and optionally `/ha/blob-readiness`) from a fixture. */
async function mockHA(
  page: Page,
  status: Record<string, unknown>,
  readiness?: Record<string, unknown>,
) {
  await page.route('**/api/terrapod/v1/ha/status', (route: Route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ data: { type: 'ha-status', id: 'node-a', attributes: status } }),
    }),
  );
  if (readiness) {
    await page.route('**/api/terrapod/v1/ha/blob-readiness*', (route: Route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          data: { type: 'ha-blob-readiness', id: 'node-a', attributes: readiness },
        }),
      }),
    );
  }
}

test.describe('HA page — role', () => {
  test('a leader with a healthy peer renders in sync', async ({ page }) => {
    await mockHA(page, BASE_STATUS);
    await page.goto('/admin/ha');

    await expect(page.getByRole('heading', { name: 'High availability' })).toBeVisible();
    await expect(page.getByText('Leader', { exact: true })).toBeVisible();
    await expect(page.getByText('Caught up as of the last successful pull.')).toBeVisible();
  });

  test('a follower is unmistakable', async ({ page }) => {
    // Reading the wrong node's UI as the leader is the mistake this prevents,
    // so the role and its consequence both have to be on screen.
    await mockHA(page, { ...BASE_STATUS, role: 'follower' });
    await page.goto('/admin/ha');

    await expect(page.getByText('Follower', { exact: true })).toBeVisible();
    await expect(page.getByText(/originates nothing/)).toBeVisible();
    await expect(page.getByText(/503/)).toBeVisible();
  });

  test('an unknown role renders as itself rather than crashing the page', async ({ page }) => {
    // A page read during an incident must not be taken down by a value the
    // catalog happens not to know.
    await mockHA(page, { ...BASE_STATUS, role: 'something-new' });
    await page.goto('/admin/ha');

    await expect(page.getByText('something-new')).toBeVisible();
  });
});

test.describe('HA page — replication', () => {
  test('backfilling is NOT in sync, however fresh the last cycle', async ({ page }) => {
    // The load-bearing assertion. `seconds-since-last-sync` is 4 — a green tick
    // beside that timestamp would be the wrong answer while a class is still
    // catching up, and it is exactly the wrong answer an operator would act on.
    await mockHA(page, {
      ...BASE_STATUS,
      'in-sync': false,
      'seconds-since-last-sync': 4,
      'backfilling-classes': ['registry_modules'],
    });
    await page.goto('/admin/ha');

    await expect(page.getByText(/Not in sync/)).toBeVisible();
    await expect(page.getByText('Caught up as of the last successful pull.')).toHaveCount(0);
    await expect(page.getByText('registry_modules')).toBeVisible();
  });

  test('a single node says so plainly instead of a wall of unknowns', async ({ page }) => {
    await mockHA(page, { ...BASE_STATUS, 'peer-configured': false });
    await page.goto('/admin/ha');

    await expect(page.getByRole('heading', { name: 'Single node' })).toBeVisible();
    await expect(page.getByText(/ha\.peer\.url/)).toBeVisible();
    // No peer means the replication panel is meaningless — it must not render.
    await expect(page.getByRole('heading', { name: 'Settings replication' })).toHaveCount(0);
  });

  test('a configured peer with replication off says that, not "in sync"', async ({ page }) => {
    await mockHA(page, { ...BASE_STATUS, 'replication-enabled': false });
    await page.goto('/admin/ha');

    await expect(page.getByText(/replication is switched off/)).toBeVisible();
    await expect(page.getByText('Caught up as of the last successful pull.')).toHaveCount(0);
  });
});

test.describe('HA page — object store readiness', () => {
  test('nothing is checked until asked', async ({ page }) => {
    // The check makes real object-store round trips, which is why it is not on
    // `/ha/status` and not polled. If results rendered on load, that design
    // would have been undone.
    let calls = 0;
    await page.route('**/api/terrapod/v1/ha/blob-readiness*', (route: Route) => {
      calls += 1;
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          data: { type: 'ha-blob-readiness', id: 'node-a', attributes: BASE_READINESS },
        }),
      });
    });
    await mockHA(page, BASE_STATUS);
    await page.goto('/admin/ha');

    await expect(page.getByRole('heading', { name: 'Object store' })).toBeVisible();
    await expect(page.getByText('state', { exact: true })).toHaveCount(0);
    expect(calls, 'blob readiness must not run on page load').toBe(0);

    await page.getByRole('button', { name: 'Check now' }).click();
    await expect(page.getByText('state', { exact: true }).first()).toBeVisible();
    expect(calls).toBe(1);
  });

  test('a sampled result is never presented as a verdict on the estate', async ({ page }) => {
    await mockHA(page, BASE_STATUS, BASE_READINESS);
    await page.goto('/admin/ha');
    await page.getByRole('button', { name: 'Check now' }).click();

    await expect(page.getByText(/^Sampled —/)).toBeVisible();
    // …and the per-class counts have to make the sample's size legible, so
    // "0 missing" can be read against how little was actually looked at.
    await expect(page.getByText('20 / 400').first()).toBeVisible();
  });

  test('an unchecked irreplaceable class is not a pass', async ({ page }) => {
    // Zero missing out of zero checked is the failure mode this line exists to
    // stop an operator reading as a green light.
    await mockHA(page, BASE_STATUS, {
      ...BASE_READINESS,
      'irreplaceable-unchecked': ['state'],
    });
    await page.goto('/admin/ha');
    await page.getByRole('button', { name: 'Check now' }).click();

    await expect(page.getByText(/No claim was made about/)).toBeVisible();
    await expect(page.getByText(/zero missing does not mean present/)).toBeVisible();
  });

  test('missing irreplaceable objects say do not fail over', async ({ page }) => {
    await mockHA(page, BASE_STATUS, {
      ...BASE_READINESS,
      'missing-total': 3,
      'irreplaceable-missing': ['state'],
      classes: [{ ...BASE_READINESS.classes[0], missing: 3 }],
    });
    await page.goto('/admin/ha');
    await page.getByRole('button', { name: 'Check now' }).click();

    await expect(page.getByText(/Do not fail over onto this node/)).toBeVisible();
  });

  test('an unreadable store reports the reason instead of an empty table', async ({ page }) => {
    await mockHA(page, BASE_STATUS, {
      ...BASE_READINESS,
      'unavailable-reason': 'connection refused',
      classes: [],
    });
    await page.goto('/admin/ha');
    await page.getByRole('button', { name: 'Check now' }).click();

    await expect(page.getByText(/connection refused/)).toBeVisible();
  });
});

test.describe('HA page — mobile', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('renders at a phone viewport without horizontal scroll', async ({ page }) => {
    await mockHA(page, {
      ...BASE_STATUS,
      role: 'follower',
      'in-sync': false,
      'backfilling-classes': ['registry_modules', 'registry_providers'],
    }, BASE_READINESS);
    await page.goto('/admin/ha');

    await expect(page.getByText('Follower', { exact: true })).toBeVisible();
    await expectNoHorizontalPageScroll(page);

    await page.getByRole('button', { name: 'Check now' }).click();
    // The desktop table is hidden; the same rows render as cards, and the
    // primary signal (class, tier, missing count) stays visible rather than
    // being hidden behind a breakpoint.
    await expect(page.locator('table')).toBeHidden();
    await expect(page.getByText('state', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Irreplaceable').first()).toBeVisible();
    await expect(page.getByText(/Missing: 0/)).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });
});

test.describe('HA page — RBAC', () => {
  test.use({ storageState: USER_AUTH });

  test('a regular user gets no nav link and no data', async ({ page }) => {
    await page.goto('/workspaces');
    await expect(page.locator('a[href="/admin/ha"]')).toHaveCount(0);

    // Direct navigation is answered by the API, not by the client: the endpoint
    // requires admin-or-audit, so the page surfaces the refusal rather than
    // rendering a role it was never told.
    await page.goto('/admin/ha');
    await expect(page.getByText('Leader', { exact: true })).toHaveCount(0);
    await expect(page.getByText('Follower', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Check now' })).toHaveCount(0);
  });
});
