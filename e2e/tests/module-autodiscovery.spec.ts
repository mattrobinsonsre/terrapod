import { test, expect, type Page } from '@playwright/test';
import { expectNoHorizontalPageScroll } from '../helpers/responsive';

// Admin → Module autodiscovery (#1584). The e2e stack has no repository to read,
// so the VCS connection, the rules API and the repository preview are stubbed;
// the tests assert what the page sends and what it shows.

const RULE_ID = 'modrule-0198e2e0-0000-7000-8000-000000000001';
const REPO = 'https://github.com/e2e-org/terraform-azurerm-mg';

function ruleJson(name: string) {
  return {
    id: RULE_ID,
    type: 'module-autodiscovery-rules',
    attributes: {
      name,
      'vcs-connection-id': 'vcs-e2e',
      'repo-url': REPO,
      branch: '',
      pattern: '**/*.tf',
      'ignore-patterns': [],
      enabled: true,
      'name-template': '',
      provider: '',
      'vcs-tag-pattern': 'v*',
      labels: {},
      'owner-email': '',
      'first-scan-at': null,
      'last-scanned-sha': '',
      'created-at': '2026-09-14T00:00:00Z',
      'updated-at': '2026-09-14T00:00:00Z',
    },
  };
}

const PAGE_META = { pagination: { 'current-page': 1, 'page-size': 1, 'total-count': 1, 'total-pages': 1 } };

const PREVIEW = {
  data: {
    type: 'module-autodiscovery-rule-previews',
    attributes: {
      ref: 'main',
      'files-walked': 4,
      entries: [
        { subdirectory: '', name: 'mg', provider: 'azurerm', 'registered-as': { name: 'mg', provider: 'azurerm' }, collision: false, 'missing-provider': false },
        { subdirectory: 'modules/create', name: 'mg-create', provider: 'azurerm', 'registered-as': null, collision: false, 'missing-provider': false },
        { subdirectory: 'modules/update', name: 'mg-update', provider: 'azurerm', 'registered-as': null, collision: true, 'missing-provider': false },
      ],
    },
  },
};

async function stubRules(page: Page, rules: () => unknown[]) {
  await page.route(
    (url) => url.pathname === '/api/terrapod/v1/vcs-connections',
    (route) =>
      route.fulfill({
        json: {
          data: [{ id: 'vcs-e2e', type: 'vcs-connections', attributes: { name: 'e2e-github', provider: 'github' } }],
          meta: PAGE_META,
        },
      }),
  );
  await page.route(
    (url) => url.pathname === '/api/terrapod/v1/module-autodiscovery-rules',
    (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      return route.fulfill({ json: { data: rules(), meta: PAGE_META } });
    },
  );
}

test.describe('Admin — Module autodiscovery (#1584)', () => {
  test('create a rule through the form', async ({ page }) => {
    let created: Record<string, unknown> | null = null;
    const rules: unknown[] = [];
    await stubRules(page, () => rules);
    await page.route(
      (url) => url.pathname === '/api/terrapod/v1/module-autodiscovery-rules',
      async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        created = route.request().postDataJSON().data.attributes;
        rules.push(ruleJson('azure-mg'));
        await route.fulfill({ status: 201, json: { data: ruleJson('azure-mg') } });
      },
    );

    await page.goto('/admin/module-autodiscovery');
    await expect(page.getByRole('heading', { name: 'Module autodiscovery', level: 1 })).toBeVisible({ timeout: 15_000 });
    const form = page.getByRole('heading', { name: 'New module autodiscovery rule' });
    await expect(async () => {
      if (!(await form.isVisible())) await page.getByRole('button', { name: 'New Rule' }).click();
      await expect(form).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });

    await page.getByLabel('Name', { exact: true }).fill('azure-mg');
    await page.getByLabel('VCS connection').selectOption('vcs-e2e');
    await page.getByLabel('Repo URL').fill(REPO);
    await page.getByLabel('Ignore patterns (one per line)').fill('modules/legacy/**');
    await page.getByLabel('Provider (optional)').fill('azurerm');
    await page.getByRole('button', { name: 'Create', exact: true }).click();

    await expect.poll(() => created).not.toBeNull();
    expect(created).toMatchObject({
      name: 'azure-mg',
      'vcs-connection-id': 'vcs-e2e',
      'repo-url': REPO,
      pattern: '**/*.tf',
      'ignore-patterns': ['modules/legacy/**'],
      provider: 'azurerm',
      'vcs-tag-pattern': 'v*',
      enabled: true,
    });
    await expect(page.getByText('Created azure-mg')).toBeVisible();
  });

  test('preview a saved rule and register only the ticked module', async ({ page }) => {
    await stubRules(page, () => [ruleJson('azure-mg')]);
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${RULE_ID}/preview`,
      (route) => route.fulfill({ json: PREVIEW }),
    );
    let scanned: Record<string, unknown> | null = null;
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${RULE_ID}/scan`,
      async (route) => {
        scanned = route.request().postDataJSON().data.attributes;
        await route.fulfill({
          json: {
            data: {
              type: 'module-autodiscovery-rule-scans',
              attributes: {
                ref: 'main',
                'files-walked': 4,
                'modules-registered': 1,
                modules: [{ id: 'm1', name: 'mg-create', provider: 'azurerm', subdirectory: 'modules/create' }],
                skipped: [],
              },
            },
          },
        });
      },
    );

    await page.goto('/admin/module-autodiscovery');
    const panel = page.getByRole('heading', { name: 'Preview: azure-mg' });
    await expect(async () => {
      if (!(await panel.isVisible())) await page.getByRole('button', { name: 'Preview' }).first().click();
      await expect(panel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });

    // Registered and name-taken candidates can't be ticked, and say why.
    await expect(page.getByText('Already registered as mg/azurerm')).toBeVisible();
    await expect(page.getByText('Name already taken by another module')).toBeVisible();
    await expect(page.getByRole('checkbox', { name: 'Register the module in (repository root)' })).toBeDisabled();
    await expect(page.getByRole('checkbox', { name: 'Register the module in modules/update' })).toBeDisabled();

    await page.getByRole('checkbox', { name: 'Register the module in modules/create' }).check();
    await page.getByRole('button', { name: 'Register 1 module' }).click();

    await expect.poll(() => scanned).not.toBeNull();
    expect(scanned).toEqual({ subdirectories: ['modules/create'] });
    await expect(page.getByText('Registered 1 module.')).toBeVisible();
  });
});

test.describe('Admin — Module autodiscovery at phone width (#1584)', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test('rules list and preview fit a phone', async ({ page }) => {
    await stubRules(page, () => [ruleJson('a-rule-with-a-rather-long-name-that-has-to-wrap')]);
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${RULE_ID}/preview`,
      (route) => route.fulfill({ json: PREVIEW }),
    );

    await page.goto('/admin/module-autodiscovery');
    // The phone layout is cards: status and repository are shown, not hidden.
    // The desktop table is still in the DOM (hidden with CSS), so every locator
    // is scoped to the card, and no table may be visible at this width.
    const card = page.getByRole('listitem').filter({ hasText: 'a-rule-with-a-rather-long-name-that-has-to-wrap' });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card.getByText('enabled', { exact: true })).toBeVisible();
    await expect(page.locator('table:visible')).toHaveCount(0);
    await expectNoHorizontalPageScroll(page);

    const panel = page.getByRole('heading', { name: /^Preview: / });
    await expect(async () => {
      if (!(await panel.isVisible())) await card.getByRole('button', { name: 'Preview' }).click();
      await expect(panel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
    await expect(page.getByRole('checkbox', { name: 'Register the module in modules/create' })).toBeEnabled();
    await expectNoHorizontalPageScroll(page);
  });
});
