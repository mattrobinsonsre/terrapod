import { test, expect } from '@playwright/test';

test.describe('Audit Log', () => {
  test('audit log page loads with entries', async ({ page }) => {
    await page.goto('/admin/audit-log');
    await expect(page.locator('h1:has-text("Audit Log")')).toBeVisible();

    // Previous test activity should have generated audit entries.
    // At minimum the table should have at least one row.
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });
  });

  test('apply method filter narrows results', async ({ page }) => {
    await page.goto('/admin/audit-log');
    await expect(page.locator('h1:has-text("Audit Log")')).toBeVisible();

    // Wait for initial data
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // Apply the POST filter and wait for the *filtered* response to land before
    // asserting — otherwise the badge check races the table refresh and reads
    // the pre-filter (mixed-method) rows, which is what made this test flaky
    // (#240). Tying the assertion to the request the click triggers makes it
    // deterministic regardless of CI latency.
    await page.selectOption('#f-action', 'POST');
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/admin/audit-log') && r.url().includes('action') && r.ok()
      ),
      page.click('button:has-text("Apply Filters")'),
    ]);

    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // Assert there is no surviving row whose action cell is not POST. toHaveCount
    // auto-retries, so it keeps polling until the table has fully re-rendered
    // to the filtered set rather than snapshotting it once.
    const nonPost = page.locator('table tbody tr td:nth-child(3)').filter({ hasNotText: 'POST' });
    await expect(nonPost).toHaveCount(0, { timeout: 10_000 });
  });

  test('clear filters restores full list', async ({ page }) => {
    await page.goto('/admin/audit-log');
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // Apply a filter first
    await page.selectOption('#f-action', 'DELETE');
    await page.click('button:has-text("Apply Filters")');

    // Now clear
    await page.click('button:has-text("Clear")');

    // Table should reload with entries (broader set)
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // The method select should be reset (empty / "All" value)
    const val = await page.locator('#f-action').inputValue();
    expect(val).toBe('');

    // Clear resets which field the actor box means, too -- a left-over "VCS
    // Login" would silently re-filter the next search against the wrong column.
    expect(await page.locator('#f-actor-kind').inputValue()).toBe('email');
  });

  test('the actor box drives both served actor filters (#2701)', async ({ page }) => {
    // `filter[actor-login]` has been served since #2669 and was reachable only
    // by hand-editing a URL: the box drove `filter[actor]` alone. The two are
    // exact-match and the server ANDs them, so one value must be sent as ONE
    // of them -- sending both would ask for a row whose email AND login are
    // that value, and no row has both.
    await page.goto('/admin/audit-log');
    await expect(page.locator('h1:has-text("Audit Log")')).toBeVisible();
    await expect(page.locator('table tbody tr').first()).toBeVisible({ timeout: 10_000 });

    // Default kind: the email filter, which is what the box did before.
    await page.fill('#f-actor', 'admin@example.com');
    const [emailReq] = await Promise.all([
      page.waitForResponse((r) => r.url().includes('/admin/audit-log') && r.ok()),
      page.click('button:has-text("Apply Filters")'),
    ]);
    expect(decodeURIComponent(emailReq.url())).toContain('filter[actor]=admin%40example.com');
    expect(decodeURIComponent(emailReq.url())).not.toContain('filter[actor-login]');

    // The VCS login filter, which nothing in the UI could reach before.
    await page.selectOption('#f-actor-kind', 'login');
    await page.fill('#f-actor', 'octocat');
    const [loginReq] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/admin/audit-log') && r.url().includes('actor-login') && r.ok()
      ),
      page.click('button:has-text("Apply Filters")'),
    ]);
    const loginUrl = decodeURIComponent(loginReq.url());
    expect(loginUrl).toContain('filter[actor-login]=octocat');
    // Only ONE of the two, or the AND above matches nothing.
    expect(loginUrl).not.toContain('filter[actor]=');
  });

  test('a VCS actor is badged and a Terrapod user is not (#2701)', async ({ page }) => {
    // `actor-type` is served and was surfaced nowhere, so an action driven from
    // a pull-request comment and one taken in the UI read identically. There is
    // no API to write an audit entry, so the three actor types are stubbed:
    // what is under test is the render, and the route is the real one the page
    // calls through the BFF.
    await page.route('**/api/terrapod/v1/admin/audit-log**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: [
            {
              id: '11111111-1111-1111-1111-111111111111',
              type: 'audit-log-entries',
              attributes: {
                timestamp: '2026-01-01T00:00:00Z',
                'actor-email': '', 'actor-type': 'vcs_user',
                'actor-login': 'e2e-vcs-login', 'actor-id': '4242',
                origin: 'pr_comment', 'actor-ip': '10.0.0.1',
                action: 'apply', 'resource-type': 'workspaces',
                'resource-id': 'ws-1', 'status-code': 200,
                'request-id': 'r1', 'duration-ms': 12, detail: '',
              },
            },
            {
              id: '22222222-2222-2222-2222-222222222222',
              type: 'audit-log-entries',
              attributes: {
                timestamp: '2026-01-01T00:00:01Z',
                'actor-email': 'e2e-human@example.com', 'actor-type': 'terrapod_user',
                'actor-login': '', 'actor-id': '',
                origin: 'terrapod_ui', 'actor-ip': '10.0.0.2',
                action: 'POST', 'resource-type': 'workspaces',
                'resource-id': 'ws-1', 'status-code': 201,
                'request-id': 'r2', 'duration-ms': 34, detail: '',
              },
            },
            {
              id: '33333333-3333-3333-3333-333333333333',
              type: 'audit-log-entries',
              attributes: {
                timestamp: '2026-01-01T00:00:02Z',
                'actor-email': '', 'actor-type': 'system',
                'actor-login': '', 'actor-id': '',
                origin: 'system', 'actor-ip': '',
                action: 'DELETE', 'resource-type': 'workspaces',
                'resource-id': 'ws-2', 'status-code': 200,
                'request-id': 'r3', 'duration-ms': 56, detail: '',
              },
            },
          ],
          meta: { pagination: { 'current-page': 1, 'page-size': 20, 'total-count': 3, 'total-pages': 1 } },
        }),
      })
    );

    await page.goto('/admin/audit-log');
    await expect(page.locator('h1:has-text("Audit Log")')).toBeVisible();

    const vcsRow = page.getByRole('row').filter({ hasText: 'e2e-vcs-login' });
    await expect(vcsRow).toBeVisible({ timeout: 10_000 });
    await expect(vcsRow.getByTestId('actor-type-badge')).toHaveText('VCS');

    // The ordinary case carries NO badge -- badging it would badge almost every
    // row and bury the one the log could not distinguish before.
    const humanRow = page.getByRole('row').filter({ hasText: 'e2e-human@example.com' });
    await expect(humanRow).toBeVisible();
    await expect(humanRow.getByTestId('actor-type-badge')).toHaveCount(0);

    // `system` is badged too: it has neither an email nor a login, so the cell
    // was a bare dash that read as missing data rather than as the platform.
    const systemRow = page.getByRole('row').filter({ hasText: 'ws-2' });
    await expect(systemRow.getByTestId('actor-type-badge')).toHaveText('System');
  });
});
