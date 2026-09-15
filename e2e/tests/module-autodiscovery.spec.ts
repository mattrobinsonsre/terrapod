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

    // #1625: while the form is open, its own Cancel is the only one; the header
    // does not become a second one.
    await expect(page.getByRole('button', { name: 'Cancel' })).toHaveCount(1);
    await expect(page.getByText(/Copied onto every module this rule registers/)).toBeVisible();

    await page.getByLabel('Rule name').fill('azure-mg');
    await page.getByLabel('VCS connection').selectOption('vcs-e2e');
    await page.getByLabel('Repo URL').fill(REPO);
    await page.getByLabel('Ignore patterns (one per line)').fill('modules/legacy/**');
    await page.getByLabel('Provider (optional)').fill('azurerm');

    // #1625: Enter in the labels editor adds the label and does not submit the form.
    await page.getByPlaceholder('key').fill('team');
    await page.getByPlaceholder('value').fill('platform');
    await page.getByPlaceholder('value').press('Enter');
    await expect(page.getByRole('button', { name: 'Remove team' })).toBeVisible();
    expect(created).toBeNull();

    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${RULE_ID}/preview`,
      (route) => route.fulfill({ json: PREVIEW }),
    );
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
      labels: { team: 'platform' },
      enabled: true,
    });
    await expect(page.getByText('Created azure-mg')).toBeVisible();

    // #1625: the saved rule's preview opens straight away, ready to register.
    await expect(page.getByRole('heading', { name: 'Preview: azure-mg' })).toBeVisible();
    await expect(page.getByRole('checkbox', { name: 'Register the module in modules/create' })).toBeEnabled();
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

// ── Org-wide rules (#1620) ────────────────────────────────────────────

const ORG_RULE_ID = 'modrule-0198e2e0-0000-7000-8000-000000000002';
const ORG_ERROR = 'the listing stopped at 1000 repositories, so no repository is marked out of scope';

function orgRuleJson() {
  return {
    ...ruleJson('org-rule'),
    id: ORG_RULE_ID,
    attributes: {
      ...ruleJson('org-rule').attributes,
      'repo-url': 'https://github.com/e2e-org/terraform-*',
      'target-kind': 'pattern',
      'last-enumerated-at': '2026-09-15T10:00:00Z',
      'last-error': ORG_ERROR,
    },
  };
}

function entry(repository: string, subdirectory: string, name: string, registered = false) {
  return {
    repository,
    'repo-url': `https://github.com/${repository}`,
    subdirectory,
    name,
    provider: 'aws',
    'registered-as': registered ? { name, provider: 'aws' } : null,
    collision: false,
    'missing-provider': false,
  };
}

function summary(repository: string, status: string, origin: string, error = '') {
  return { repository, 'repo-url': `https://github.com/${repository}`, ref: 'main', status, origin, error };
}

function orgPreview(page: number) {
  const meta = { pagination: { 'current-page': page, 'page-size': 3, 'total-count': 4, 'total-pages': 2 } };
  if (page === 2) {
    return {
      data: {
        type: 'module-autodiscovery-rule-previews',
        attributes: {
          ref: '',
          'files-walked': 0,
          'target-kind': 'pattern',
          'listing-complete': false,
          entries: [entry('e2e-org/terraform-aws-d', '', 'd')],
          repositories: [summary('e2e-org/terraform-aws-d', 'active', 'baseline')],
        },
      },
      meta,
    };
  }
  return {
    data: {
      type: 'module-autodiscovery-rule-previews',
      attributes: {
        ref: '',
        'files-walked': 0,
        'target-kind': 'pattern',
        'listing-complete': false,
        entries: [
          entry('e2e-org/terraform-aws-a', '', 'a'),
          entry('e2e-org/terraform-aws-a', 'modules/x', 'a-x'),
          entry('e2e-org/terraform-aws-b', '', 'b', true),
        ],
        repositories: [
          summary('e2e-org/terraform-aws-a', 'active', 'new'),
          summary('e2e-org/terraform-aws-b', 'active', 'baseline'),
          summary('e2e-org/terraform-aws-c', 'error', 'baseline', 'tree listing failed'),
        ],
      },
    },
    meta,
  };
}

function repositoryRow(repository: string, status: string, origin: string, error = '') {
  return {
    id: `modrepo-${repository.split('/')[1]}`,
    type: 'module-autodiscovery-rule-repositories',
    attributes: {
      repository,
      'repo-url': `https://github.com/${repository}`,
      'vcs-repo-id': '1',
      'default-branch': 'main',
      origin,
      status,
      'last-scanned-sha': '',
      'seen-subdirectories': [],
      candidates: status === 'active' ? [{ subdirectory: '', name: 'x', provider: 'aws' }] : [],
      'last-skips': [],
      'previous-paths': status === 'active' ? [{ path: 'e2e-org/old-name', url: 'https://github.com/e2e-org/old-name' }] : [],
      'repo-created-at': null,
      'first-seen-at': '2026-09-15T10:00:00Z',
      'last-checked-at': null,
      'next-check-at': null,
      'failure-count': error ? 2 : 0,
      'last-error': error,
    },
  };
}

async function stubRepositories(page: Page, queries: string[]) {
  await page.route(
    (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${ORG_RULE_ID}/repositories`,
    (route) => {
      const url = new URL(route.request().url());
      queries.push(url.search);
      const status = url.searchParams.get('filter[status]');
      const all = [
        repositoryRow('e2e-org/terraform-aws-a', 'active', 'new'),
        repositoryRow('e2e-org/terraform-aws-c', 'error', 'baseline', 'tree listing failed'),
        repositoryRow('e2e-org/terraform-aws-z', 'archived', 'baseline'),
      ];
      const data = status ? all.filter((r) => r.attributes.status === status) : all;
      return route.fulfill({
        json: { data, meta: { pagination: { 'current-page': 1, 'page-size': 25, 'total-count': data.length, 'total-pages': 1 } } },
      });
    },
  );
}

test.describe('Admin — Module autodiscovery, org-wide rules (#1620)', () => {
  test('grouped preview, paging, and registering a selection across repositories', async ({ page }) => {
    await stubRules(page, () => [orgRuleJson()]);
    const pagesAsked: string[] = [];
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${ORG_RULE_ID}/preview`,
      (route) => {
        const asked = new URL(route.request().url()).searchParams.get('page[number]') ?? '1';
        pagesAsked.push(asked);
        return route.fulfill({ json: orgPreview(Number(asked)) });
      },
    );
    let scanned: Record<string, unknown> | null = null;
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${ORG_RULE_ID}/scan`,
      async (route) => {
        scanned = route.request().postDataJSON().data.attributes;
        await route.fulfill({
          json: {
            data: {
              type: 'module-autodiscovery-rule-scans',
              attributes: {
                ref: '',
                'files-walked': 0,
                'modules-registered': 3,
                'repositories-scanned': 2,
                modules: [],
                skipped: [],
              },
            },
          },
        });
      },
    );

    await page.goto('/admin/module-autodiscovery');
    // The target kind and the rule's last error are shown on the list.
    await expect(page.getByRole('heading', { name: 'Module autodiscovery', level: 1 })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('org-rule needs attention')).toBeVisible();
    await expect(page.getByText(ORG_ERROR)).toBeVisible();
    // Scoped to the rule's row: "Pattern" is also the rules table's column
    // header for the file glob, so a table-wide match finds two elements.
    const orgRow = page.locator('table').getByRole('row').filter({ hasText: 'org-rule' });
    await expect(orgRow.getByText('Pattern', { exact: true })).toBeVisible();
    await expect(orgRow.getByText('Needs attention', { exact: true })).toBeVisible();

    const panel = page.getByRole('heading', { name: 'Preview: org-rule' });
    await expect(async () => {
      if (!(await panel.isVisible())) await page.locator('table').getByRole('button', { name: 'Preview' }).click();
      await expect(panel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });

    // Grouped by repository, each with its status; a repository that could not
    // be read says why, and the capped listing is called out.
    await expect(page.getByText(/What the last registry poll found in each repository/)).toBeVisible();
    await expect(page.getByText(/more repositories than Terrapod lists at once/)).toBeVisible();
    await expect(page.getByText('e2e-org/terraform-aws-c', { exact: true })).toBeVisible();
    await expect(page.getByText('Could not read this repository: tree listing failed')).toBeVisible();
    await expect(page.getByRole('checkbox', { name: 'Register the module in e2e-org/terraform-aws-b (repository root)' })).toBeDisabled();
    await expect(page.getByText('Page 1 of 2')).toBeVisible();

    await page.getByRole('checkbox', { name: 'Select every module in e2e-org/terraform-aws-a' }).check();
    await expect(page.getByRole('checkbox', { name: 'Register the module in e2e-org/terraform-aws-a/modules/x' })).toBeChecked();

    // The selection survives a page change.
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText('Page 2 of 2')).toBeVisible();
    await page.getByRole('checkbox', { name: 'Register the module in e2e-org/terraform-aws-d (repository root)' }).check();
    await page.getByRole('button', { name: 'Register 3 modules' }).click();

    await expect.poll(() => scanned).not.toBeNull();
    expect(scanned).toEqual({
      selections: [
        { repository: 'e2e-org/terraform-aws-a', subdirectories: ['', 'modules/x'] },
        { repository: 'e2e-org/terraform-aws-d', subdirectories: [''] },
      ],
    });
    expect(pagesAsked).toEqual(['1', '2']);
    await expect(page.getByText('Registered 3 modules. Scanned 2 repositories.')).toBeVisible();
  });

  test('registering everything from an org-wide rule is confirmed first', async ({ page }) => {
    await stubRules(page, () => [orgRuleJson()]);
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${ORG_RULE_ID}/preview`,
      (route) => route.fulfill({ json: orgPreview(1) }),
    );
    let scans = 0;
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${ORG_RULE_ID}/scan`,
      (route) => {
        scans++;
        return route.fulfill({ json: { data: { type: 'module-autodiscovery-rule-scans', attributes: { 'modules-registered': 0, 'repositories-scanned': 0, modules: [], skipped: [] } } } });
      },
    );

    await page.goto('/admin/module-autodiscovery');
    const panel = page.getByRole('heading', { name: 'Preview: org-rule' });
    await expect(async () => {
      if (!(await panel.isVisible())) await page.locator('table').getByRole('button', { name: 'Preview' }).click();
      await expect(panel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });

    // Declining sends nothing.
    page.once('dialog', (d) => d.dismiss());
    await page.getByRole('button', { name: 'Register all' }).click();
    await expect(panel).toBeVisible();
    expect(scans).toBe(0);
  });

  test('the repositories view shows status and origin, and filters by status', async ({ page }) => {
    await stubRules(page, () => [orgRuleJson()]);
    const queries: string[] = [];
    await stubRepositories(page, queries);

    await page.goto('/admin/module-autodiscovery');
    const panel = page.getByRole('heading', { name: 'Repositories: org-rule' });
    await expect(async () => {
      if (!(await panel.isVisible())) await page.locator('table').getByRole('button', { name: 'Repositories' }).click();
      await expect(panel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });

    // Found by a column only this table has, not by a row's text: the status
    // filter below removes that row, and a locator keyed on it would then
    // match no table at all.
    const table = page.getByRole('table').filter({ has: page.getByRole('columnheader', { name: 'Origin' }) });
    await expect(table.getByText('e2e-org/terraform-aws-c')).toBeVisible();
    await expect(table.getByText('tree listing failed')).toBeVisible();
    await expect(table.getByText('Renamed from e2e-org/old-name')).toBeVisible();
    await expect(table.getByText('new', { exact: true })).toBeVisible();
    await expect(table.getByText('archived', { exact: true })).toBeVisible();

    await page.getByLabel('Status').selectOption('error');
    await expect(table.getByText('e2e-org/terraform-aws-a')).toHaveCount(0);
    await expect(table.getByText('e2e-org/terraform-aws-c')).toBeVisible();
    // Parsed, not matched as a raw string: the page sends the brackets
    // unencoded (`filter[status]=error`), and either spelling is the same query.
    expect(new URLSearchParams(queries.at(-1) ?? '').get('filter[status]')).toBe('error');
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

  test('an org-wide rule, its repositories and its grouped preview fit a phone (#1620)', async ({ page }) => {
    await stubRules(page, () => [orgRuleJson()]);
    await stubRepositories(page, []);
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/module-autodiscovery-rules/${ORG_RULE_ID}/preview`,
      (route) => route.fulfill({ json: orgPreview(1) }),
    );

    await page.goto('/admin/module-autodiscovery');
    // The rule's card keeps its target kind and its needs-attention state; the
    // banner carrying the reason shows at this width too.
    const card = page.getByRole('listitem').filter({ hasText: 'org-rule' }).filter({ has: page.getByRole('button', { name: 'Repositories' }) });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card.getByText('Pattern', { exact: true })).toBeVisible();
    await expect(card.getByText('Needs attention', { exact: true })).toBeVisible();
    await expect(page.getByText(ORG_ERROR)).toBeVisible();
    await expectNoHorizontalPageScroll(page);

    // Repositories: cards, not a table; status and the error are not dropped.
    const reposPanel = page.getByRole('heading', { name: 'Repositories: org-rule' });
    await expect(async () => {
      if (!(await reposPanel.isVisible())) await card.getByRole('button', { name: 'Repositories' }).click();
      await expect(reposPanel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
    const repoCard = page.getByRole('listitem').filter({ hasText: 'e2e-org/terraform-aws-c' });
    await expect(repoCard).toBeVisible();
    await expect(repoCard.getByText('error', { exact: true })).toBeVisible();
    await expect(repoCard.getByText('tree listing failed')).toBeVisible();
    await expect(page.locator('table:visible')).toHaveCount(0);
    await expectNoHorizontalPageScroll(page);

    // The grouped preview wraps long repository paths and keeps tap targets.
    const previewPanel = page.getByRole('heading', { name: 'Preview: org-rule' });
    await expect(async () => {
      if (!(await previewPanel.isVisible())) await card.getByRole('button', { name: 'Preview' }).click();
      await expect(previewPanel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
    await expect(page.getByRole('checkbox', { name: 'Select every module in e2e-org/terraform-aws-a' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Next' })).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });
});
