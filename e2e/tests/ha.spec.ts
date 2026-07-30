import { test, expect, type Page, type Route } from '@playwright/test';
import path from 'path';
import { expectNoHorizontalPageScroll } from '../helpers/responsive';

/**
 * The high-availability admin page (#1163).
 *
 * Driven against mocked `/ha/*` responses rather than the live stack. That is
 * deliberate and is the only way this suite earns its keep: the e2e stack is a
 * healthy single node, so every state this page exists to make legible — a
 * follower banner, a class mid-backfill, replication switched off — is
 * unreachable without a fixture. Testing it against the real single-node stack
 * would assert only that the happy path renders, which is precisely the case
 * that cannot mislead anyone.
 *
 * The property this leans on hardest: a class still backfilling means NOT in
 * sync, however fresh the last cycle was.
 */

const USER_AUTH = path.join(__dirname, '..', '.auth', 'user.json');

/** The page body, excluding the nav chip that now carries the same word. */
const body = (page: Page) => page.getByRole('main');
/** The nav-bar HA chip (#1165). */
const chip = (page: Page) => page.getByRole('navigation').getByRole('link', { name: /High availability/i });

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

/** Serve `/ha/status` from a fixture. */
async function mockHA(page: Page, status: Record<string, unknown>) {
  await page.route('**/api/terrapod/v1/ha/status', (route: Route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ data: { type: 'ha-status', id: 'node-a', attributes: status } }),
    }),
  );
}

test.describe('HA page — role', () => {
  test('a leader with a healthy peer renders in sync', async ({ page }) => {
    await mockHA(page, BASE_STATUS);
    await page.goto('/ha');

    await expect(page.getByRole('heading', { name: 'High availability' })).toBeVisible();
    await expect(body(page).getByText('Leader', { exact: true })).toBeVisible();
    await expect(page.getByText('Caught up as of the last successful pull.')).toBeVisible();
  });

  test('a follower is unmistakable', async ({ page }) => {
    // Reading the wrong node's UI as the leader is the mistake this prevents,
    // so the role and its consequence both have to be on screen.
    await mockHA(page, { ...BASE_STATUS, role: 'follower' });
    await page.goto('/ha');

    await expect(body(page).getByText('Follower', { exact: true })).toBeVisible();
    await expect(page.getByText(/originates nothing/)).toBeVisible();
    await expect(page.getByText(/503/)).toBeVisible();
  });

  test('an unknown role renders as itself rather than crashing the page', async ({ page }) => {
    // A page read during an incident must not be taken down by a value the
    // catalog happens not to know.
    await mockHA(page, { ...BASE_STATUS, role: 'something-new' });
    await page.goto('/ha');

    await expect(body(page).getByText('something-new')).toBeVisible();
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
    await page.goto('/ha');

    await expect(page.getByText(/Not in sync/)).toBeVisible();
    await expect(page.getByText('Caught up as of the last successful pull.')).toHaveCount(0);
    await expect(page.getByText('registry_modules')).toBeVisible();
  });

  test('a single node says so plainly instead of a wall of unknowns', async ({ page }) => {
    await mockHA(page, { ...BASE_STATUS, 'peer-configured': false });
    await page.goto('/ha');

    await expect(page.getByRole('heading', { name: 'Single node' })).toBeVisible();
    await expect(page.getByText(/ha\.peer\.url/)).toBeVisible();
    // No peer means the replication panel is meaningless — it must not render.
    await expect(page.getByRole('heading', { name: 'Settings replication' })).toHaveCount(0);
  });

  test('a configured peer with replication off says that, not "in sync"', async ({ page }) => {
    await mockHA(page, { ...BASE_STATUS, 'replication-enabled': false });
    await page.goto('/ha');

    await expect(page.getByText(/replication is switched off/)).toBeVisible();
    await expect(page.getByText('Caught up as of the last successful pull.')).toHaveCount(0);
  });
});

test.describe('HA indicator (nav bar)', () => {
  test('it is on an ordinary page, carries the role, and opens the HA page', async ({ page }) => {
    // The whole point of #1165: you learn which node you are on without
    // navigating anywhere, from any page.
    await mockHA(page, BASE_STATUS);
    await page.goto('/workspaces');

    await expect(chip(page)).toBeVisible();
    await expect(chip(page)).toContainText('Leader');

    await chip(page).click();
    await expect(page).toHaveURL(/\/ha$/);
  });

  test('a follower is stated in the chip, not only on the page', async ({ page }) => {
    await mockHA(page, { ...BASE_STATUS, role: 'follower' });
    await page.goto('/workspaces');

    await expect(chip(page)).toContainText('Follower');
    // Colour is not the only signal: the accessible name carries the detail
    // too, so the state survives a monochrome screenshot and a screen reader.
    await expect(chip(page)).toHaveAttribute('aria-label', /Follower/);
  });

  test('it is not in the Admin menu', async ({ page }) => {
    // Node disposition is context, not an administrative task. If it reappears
    // under Admin, the permission story has drifted back too.
    await mockHA(page, BASE_STATUS);
    await page.goto('/workspaces');

    await expect(page.locator('a[href="/admin/ha"]')).toHaveCount(0);
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
    });
    await page.goto('/ha');

    // The primary signal — which node this is, and that it is behind — stays
    // visible at phone width rather than being hidden behind a breakpoint.
    await expect(body(page).getByText('Follower', { exact: true })).toBeVisible();
    await expect(page.getByText(/Not in sync/)).toBeVisible();
    await expect(page.getByText('registry_modules')).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });
});

test.describe('HA page — RBAC', () => {
  test.use({ storageState: USER_AUTH });

  test('a regular user reaches the page and sees the node role', async ({ page }) => {
    // #1165 reversed the earlier gate: which node you are talking to is not
    // privileged information, and the person about to be refused a write is
    // precisely who needs it.
    await mockHA(page, { ...BASE_STATUS, role: 'follower', 'components-restricted': true });
    await page.goto('/ha');

    await expect(body(page).getByText('Follower', { exact: true })).toBeVisible();
    await expect(page.getByText(/originates nothing/)).toBeVisible();
  });

  test('the cluster half is withheld rather than shown empty', async ({ page }) => {
    // An empty component list would read as "nothing is running". Restricted
    // means the caller may not ask — a different statement, and the page has to
    // make it rather than imply the other one.
    await mockHA(page, { ...BASE_STATUS, 'components-restricted': true, components: [] });
    await page.goto('/ha');

    await expect(page.getByText(/not shown/i)).toBeVisible();
    await expect(page.getByText('No components reported.')).toHaveCount(0);
  });
});
