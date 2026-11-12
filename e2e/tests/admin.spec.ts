import { test, expect } from '../fixtures/auth.js';
import type { Dialog } from '@playwright/test';
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api.js';

test.describe('Admin access control', () => {
  test('admin user sees admin nav links', async ({ adminPage }) => {
    await adminPage.goto('/workspaces');
    await expect(adminPage.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Admin destinations now live behind the Admin▾ dropdown (#719 IA):
    // the trigger is visible, and opening it reveals the admin items.
    await expect(adminPage.getByRole('button', { name: 'Admin', exact: true })).toBeVisible();
    await adminPage.getByRole('button', { name: 'Admin', exact: true }).click();
    await expect(adminPage.getByRole('menuitem', { name: 'Roles' })).toBeVisible();
    await expect(adminPage.getByRole('menuitem', { name: 'Users' })).toBeVisible();
    await adminPage.keyboard.press('Escape');
    // Agent Pools stays top-level (visible to all users).
    await expect(
      adminPage.getByRole('navigation').getByRole('link', { name: 'Agents', exact: true }),
    ).toBeVisible();
  });

  test('regular user does not see admin nav links', async ({ userPage }) => {
    await userPage.goto('/workspaces');
    await expect(userPage.locator('h1:has-text("Workspaces")')).toBeVisible();

    // A non-admin/non-audit user gets no Admin▾ menu at all.
    await expect(userPage.getByRole('button', { name: 'Admin', exact: true })).not.toBeVisible();
    // Agent Pools is visible to all users (RBAC-filtered server-side)
    await expect(
      userPage.getByRole('navigation').getByRole('link', { name: 'Agents', exact: true }),
    ).toBeVisible();
  });

  test('admin can access roles page', async ({ adminPage }) => {
    await adminPage.goto('/admin/roles');

    await expect(adminPage.locator('h1:has-text("Roles")')).toBeVisible();
  });

  test('regular user is redirected from admin pages', async ({ userPage }) => {
    await userPage.goto('/admin/roles');

    // Should either show an error or redirect away from admin
    // The page checks auth and roles client-side — wait a moment for redirect
    await userPage.waitForTimeout(3_000);

    // Either redirected to home/workspaces or shows forbidden content
    const hasAdminContent = await userPage.locator('h1:has-text("Roles")').isVisible().catch(() => false);

    // Regular user should NOT see the admin roles content
    expect(hasAdminContent).toBe(false);
  });
});

test.describe('Cache bulk-warm (#606)', () => {
  test('admin can open the warm panel and a bad provider source surfaces an error', async ({ adminPage }) => {
    await adminPage.goto('/admin/binary-cache');
    await expect(adminPage.locator('h1:has-text("Cache")')).toBeVisible();

    // Expand the collapsible "Warm cache" panel.
    await adminPage.getByRole('button', { name: 'Warm cache' }).click();
    const providers = adminPage.locator('#warm-providers');
    await expect(providers).toBeVisible();

    // A provider source must be hostname/namespace/type — a bare name is
    // rejected (422) by the server before any upstream fetch, so this is
    // deterministic without egress.
    await providers.fill('aws 5.60.0');
    await adminPage.getByRole('button', { name: 'Warm', exact: true }).click();

    // The validation failure surfaces in the error banner.
    await expect(adminPage.locator('text=/Warm failed|hostname\\/namespace\\/type/i')).toBeVisible({
      timeout: 15_000,
    });
  });

  test('regular user is redirected from the cache admin page', async ({ userPage }) => {
    await userPage.goto('/admin/binary-cache');
    await userPage.waitForTimeout(3_000);
    const hasContent = await userPage
      .locator('h1:has-text("Cache")')
      .isVisible()
      .catch(() => false);
    expect(hasContent).toBe(false);
  });
});

test.describe('Package and image cache warming (#2486)', () => {
  test('the panel says where it would fetch from, and what it would do, before doing it', async ({
    adminPage,
  }) => {
    // The two halves of the feature that are easy to ship without: an operator
    // could not see which upstream a submission would reach, and a submission
    // reported nothing until it had already run. Both are read from the server
    // (`/admin/package-cache/sources`) rather than hardcoded, so this also
    // proves the panel and that endpoint agree.
    await adminPage.goto('/admin/binary-cache');
    await expect(adminPage.getByTestId('cache-warm-section')).toBeVisible({ timeout: 15_000 });
    await adminPage.getByTestId('cache-warm-toggle').click();

    // Where it will pull from, as a URL rather than implied. The default
    // ecosystem is galaxy, whose configured upstream the stack ships.
    await expect(adminPage.getByTestId('cache-warm-upstream')).toContainText('galaxy.ansible.com');

    // Nothing to warm yet, so there is nothing to submit.
    await expect(adminPage.getByTestId('cache-warm-submit')).toBeDisabled();

    // Say what will happen BEFORE it happens: two lines, counted back.
    await adminPage
      .getByTestId('cache-warm-items')
      .fill('community.general 9.2.0\nansible.posix 1.5.4');
    await expect(adminPage.getByTestId('cache-warm-preview')).toContainText('2');
    await expect(adminPage.getByTestId('cache-warm-submit')).toBeEnabled();
  });

  test('pulumi without a version is refused in the form, not after submitting', async ({
    adminPage,
  }) => {
    // The server refuses the WHOLE submission for one unversioned pulumi item,
    // so an operator who pasted fifty lines would otherwise learn that from a
    // 422 with nothing warmed. `resolves-latest` comes from the server, so this
    // is the client reading that flag rather than carrying its own copy of the
    // rule.
    await adminPage.goto('/admin/binary-cache');
    await expect(adminPage.getByTestId('cache-warm-section')).toBeVisible({ timeout: 15_000 });
    await adminPage.getByTestId('cache-warm-toggle').click();

    await adminPage.selectOption('#warm-ecosystem', 'pulumi');
    await adminPage.getByTestId('cache-warm-items').fill('aws');

    await expect(adminPage.getByTestId('cache-warm-preview')).toContainText(/no version/i);
    await expect(adminPage.getByTestId('cache-warm-submit')).toBeDisabled();

    // With a version it is submittable again — the refusal is about the
    // missing version, not about pulumi being unwarmable.
    await adminPage.getByTestId('cache-warm-items').fill('aws 6.0.0');
    await expect(adminPage.getByTestId('cache-warm-submit')).toBeEnabled();
  });
});

test.describe('Bulk update — auto-apply mode (#1276)', () => {
  // This page had no functional spec at all, only a redirect check in
  // rbac-negatives. The mode select is worth covering specifically because it
  // is one of the two write paths taking TWO input spellings for one setting
  // -- `auto-apply-mode` and the superseded `auto-apply` boolean -- and the
  // form must offer the mode rather than the boolean.
  test('a conditional mode previews as a change to the auto-apply mode', async ({ adminPage }) => {
    const token = getStoredToken();
    const prefix = uniqueName('e2e-bulkmode');
    await createWorkspace(token, `${prefix}-a`);
    await createWorkspace(token, `${prefix}-b`);

    await adminPage.goto('/admin/bulk-update');

    // 1. Select by name prefix — scoped to this test's own workspaces so the
    //    assertions never depend on what else is in the org.
    await adminPage.getByPlaceholder('prod-').fill(prefix);
    await adminPage.getByRole('button', { name: 'Search' }).click();
    await expect(adminPage.getByText(/Matched/)).toContainText('2');

    // 2. The control must be the four-value mode select, not a boolean.
    const mode = adminPage.getByRole('combobox', { name: 'Auto-apply' });
    await expect(mode).toHaveValue(''); // "(unchanged)" — absent must not write
    await mode.selectOption('create_update');

    // 3. Dry run is on by default, so nothing is written by this assertion.
    await adminPage.getByRole('button', { name: 'Preview (dry run)' }).click();

    // Both workspaces are affected — the summary states it, and the diff table
    // heading is the section we then read the per-column changes out of.
    await expect(adminPage.getByText(/2 would change/)).toBeVisible({ timeout: 15_000 });
    await expect(
      adminPage.getByRole('heading', { name: /Would change \(2\)/ }),
    ).toBeVisible();

    // ONE column moves, and its value is the mode verbatim (#2503).
    //
    // The property is per COLUMN, so it has to be read per column. A bare
    // `toContainText('auto_apply')` matches the substring of `auto_apply_mode`
    // and asserts nothing either way -- and counting the diff table's `tr`
    // elements does not fix that, because a row is one WORKSPACE: both of this
    // test's two workspaces change, so a row count is 2 whatever the columns
    // say. (The first version of this assertion counted rows and expected 1,
    // with a comment claiming the row list told the two columns apart. It did
    // not: it measured the workspace dimension and would have passed for the
    // wrong reason on a single-workspace fixture.)
    //
    // So: every changed row names `auto_apply_mode`, and names no OTHER
    // auto-apply column -- which is checkable precisely because `auto_apply`
    // is a prefix of `auto_apply_mode`. If the boolean came back as its own
    // column, a row would carry one more `auto_apply` than it carries
    // `auto_apply_mode`, and the counts would differ.
    const table = adminPage.locator('table').last();
    await expect(table).toContainText('auto_apply_mode');
    await expect(table).toContainText('create_update');
    const rowTexts = await table.locator('tr', { hasText: /auto_apply/ }).allInnerTexts();
    expect(rowTexts.length, `auto-apply rows: ${JSON.stringify(rowTexts)}`).toBe(2);
    for (const row of rowTexts) {
      const named = (row.match(/auto_apply/g) ?? []).length;
      const mode = (row.match(/auto_apply_mode/g) ?? []).length;
      expect(mode, `no auto_apply_mode column in: ${row}`).toBeGreaterThan(0);
      expect(named, `a bare auto_apply column survives in: ${row}`).toBe(mode);
    }
  });
});

test.describe('Caches — the package and VCS archive sections (#2474)', () => {
  // Both caches had no admin surface at all: an operator could warm one and
  // never see what was in it, how big it had grown, or drop one bad artifact
  // without going to object storage by hand. These assertions drive the page
  // against a real (and on a fresh stack, empty) cache, which is enough to
  // prove the two listings, the two totals strips and both bulk purges reach
  // the server — the thing a component test cannot show.
  test('both sections render, and the package filter says the name is a prefix', async ({
    adminPage,
  }) => {
    await adminPage.goto('/admin/binary-cache');
    await expect(adminPage.locator('h1:has-text("Caches")')).toBeVisible({ timeout: 15_000 });

    await expect(adminPage.getByTestId('package-cache-section')).toBeVisible();
    await expect(adminPage.getByTestId('vcs-archive-cache-section')).toBeVisible();

    // The server does a PREFIX match on the name, so the placeholder says so
    // rather than leaving the operator to guess at a wildcard syntax.
    await expect(adminPage.locator('#pkg-name')).toHaveAttribute('placeholder', /prefix/i);

    // One option per ecosystem the proxy serves, plus the unfiltered choice.
    const options = adminPage.locator('#pkg-ecosystem option');
    await expect(options).toHaveCount(7);
    for (const eco of ['galaxy', 'pypi', 'npm', 'go', 'nuget', 'pulumi']) {
      await expect(adminPage.locator(`#pkg-ecosystem option[value="${eco}"]`)).toHaveCount(1);
    }

    // Both totals strips are present. They are computed over the whole cache
    // server-side, so they are totals rather than a sum of a rendered page.
    await expect(adminPage.getByText('Totals by ecosystem')).toBeVisible();
    await expect(adminPage.getByText('Totals by repository')).toBeVisible();
  });

  test('the two age filters do not say the same thing', async ({ adminPage }) => {
    // Package age is measured from the LAST ACCESS and an archive's from its
    // WRITE time, so the labels have to differ: "older than" would be wrong
    // about one of them, and an operator cannot tell which from the number.
    await adminPage.goto('/admin/binary-cache');
    await expect(adminPage.getByTestId('package-cache-section')).toBeVisible({ timeout: 15_000 });

    await expect(adminPage.getByText('Not used in this many days')).toBeVisible();
    await expect(adminPage.getByText('Written more than this many days ago')).toBeVisible();
  });

  test('a bulk purge names its scope, and cancelling it does nothing', async ({ adminPage }) => {
    await adminPage.goto('/admin/binary-cache');
    await expect(adminPage.getByTestId('vcs-archive-cache-section')).toBeVisible({ timeout: 15_000 });

    // Irreversible, so it prompts on a precise pointer too — this project runs
    // Desktop Chrome, which is what makes that the assertion here.
    let seen = '';
    const dismiss = async (d: Dialog) => { seen = d.message(); await d.dismiss(); };
    adminPage.on('dialog', dismiss);
    await adminPage.getByTestId('archive-bulk-purge').click();
    await expect.poll(() => seen, { timeout: 10_000 }).not.toBe('');
    adminPage.off('dialog', dismiss);
    expect(seen).toMatch(/every cached VCS archive/i);
    expect(seen).toMatch(/cannot be undone/i);
    // Dismissed, so nothing ran: no success banner appears.
    await expect(adminPage.getByText(/Purged \d+ archives/)).toHaveCount(0);

    // Accepting it reaches the server and the result is reported back.
    const accept = async (d: Dialog) => { await d.accept(); };
    adminPage.on('dialog', accept);
    await adminPage.getByTestId('archive-bulk-purge').click();
    await expect(adminPage.getByText(/Purged \d+ archives/)).toBeVisible({ timeout: 15_000 });
    adminPage.off('dialog', accept);
  });

  test('a package bulk purge narrowed to one ecosystem says which', async ({ adminPage }) => {
    await adminPage.goto('/admin/binary-cache');
    await expect(adminPage.getByTestId('package-cache-section')).toBeVisible({ timeout: 15_000 });

    // The purge panel has its OWN ecosystem select rather than reusing the
    // list's: the evict endpoint takes an ecosystem and an age and has no name
    // filter, so a button reading "purge what I am looking at" would purge
    // beyond a name prefix the operator had typed.
    await adminPage.locator('#pkg-purge-ecosystem').selectOption('npm');
    let seen = '';
    const accept = async (d: Dialog) => { seen = d.message(); await d.accept(); };
    adminPage.on('dialog', accept);
    await adminPage.getByTestId('package-bulk-purge').click();
    await expect(adminPage.getByText(/Purged \d+ artifacts/)).toBeVisible({ timeout: 15_000 });
    adminPage.off('dialog', accept);
    expect(seen).toMatch(/every npm artifact/i);
  });
});
