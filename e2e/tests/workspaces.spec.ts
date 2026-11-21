import { test, expect, type Page, type Route } from "@playwright/test";
import { getStoredToken, createWorkspace, lockWorkspace, seedRun, uniqueName } from "../helpers/api";
import { listOnly, stubLatestRun, NO_ACTIONS } from "../helpers/workspace-list";

// Every action offered on one stubbed run (plan-only where `planOnly(i)`); returns the URLs the bulk actions would POST to.
async function stubBulkRuns(
  page: Page,
  ids: string[],
  planOnly: (i: number) => boolean = () => false,
  isDestroy: (i: number) => boolean = () => false,
) {
  const posts: string[] = [];
  await stubLatestRun(page, ids, (i) => ({
    id: `run-stub-${i}`,
    status: "errored",
    "plan-only": planOnly(i),
    "is-destroy": isDestroy(i),
    actions: { "is-confirmable": true, "is-discardable": true, "is-cancelable": true, "is-retryable": true },
  }));
  const record = async (route: Route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posts.push(route.request().url());
    await route.fulfill({ status: 200, contentType: "application/vnd.api+json", body: "{}" });
  };
  await page.route("**/api/v1/runs", record);
  await page.route("**/api/v1/runs/*/actions/*", record);
  return posts;
}

// A completed action clears the selection, so tick the row again first.
async function runFromMenu(page: Page, action: string) {
  await page.getByTestId("ws-row-select").check();
  await page.getByTestId("ws-selection").click();
  await page.getByTestId(`ws-selection-${action}`).click();
}

test.describe("Workspaces", () => {
  test("workspace list page loads", async ({ page }) => {
    await page.goto("/workspaces");

    // Page header should be visible
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();
  });

  test("create workspace and see it in list", async ({ page }) => {
    const wsName = `e2e-ws-${Date.now()}`;

    await page.goto("/workspaces");
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Open create form
    await page.click('button:has-text("New Workspace")');

    // Fill in workspace name
    await page.fill('input[placeholder*="workspace"]', wsName);

    // Submit
    await page.click('button:has-text("Create Workspace")');

    // Workspace should appear in the list
    await expect(page.locator(`text=${wsName}`)).toBeVisible({
      timeout: 10_000,
    });
  });

  test("workspace detail shows tabs", async ({ page }) => {
    // Create a workspace first
    const wsName = `e2e-detail-${Date.now()}`;

    await page.goto("/workspaces");
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');

    // Click on the workspace to go to detail
    await page.click(`text=${wsName}`);

    // Verify tabs are present (use getByRole to avoid matching text in other elements)
    await expect(
      page.getByRole("button", { name: "Configuration" }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Variables" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Runs" })).toBeVisible();
    // `exact` so 'State' doesn't also match the 'State Graph' tab (#765).
    await expect(
      page.getByRole("button", { name: "State", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Notifications" }),
    ).toBeVisible();
  });

  test("run-triggers tab uses a searchable workspace picker", async ({
    page,
  }) => {
    const src = `e2e-trg-src-${Date.now()}`;
    const dest = `e2e-trg-dest-${Date.now()}`;

    for (const name of [src, dest]) {
      await page.goto("/workspaces");
      await page.click('button:has-text("New Workspace")');
      await page.fill('input[placeholder*="workspace"]', name);
      await page.click('button:has-text("Create Workspace")');
      await expect(page.locator(`text=${name}`).first()).toBeVisible({
        timeout: 10_000,
      });
    }

    // Open the destination workspace's Run Triggers tab. Run Triggers now lives
    // under the merged "Automation" group (#1036 tab consolidation): open the
    // group, then its "Triggers" sub-view.
    await page.goto("/workspaces");
    await page.click(`text=${dest}`);
    await page.getByRole("button", { name: "Automation", exact: true }).click();
    await page.getByRole("button", { name: "Triggers", exact: true }).click();

    // The picker is a search box + clickable list — NOT a free-text name input
    // (a typo can't 404 any more). Filter to the source, then click to add.
    const search = page.locator(
      'input[placeholder*="Search workspaces to add"]',
    );
    await expect(search).toBeVisible();
    await search.fill(src);

    const srcButton = page.getByRole("button", { name: src });
    await expect(srcButton).toBeVisible({ timeout: 10_000 });
    await srcButton.click();

    // The inbound edge appears live (id-based add → refetch), with a Remove control
    await expect(
      page.getByRole("button", { name: "Remove" }).first(),
    ).toBeVisible({
      timeout: 10_000,
    });
  });

  test("workspace filter typeahead: bare Enter keeps free text; explicit pick applies a suggestion", async ({
    page,
  }) => {
    const wsName = `e2e-flt-${Date.now()}`;

    await page.goto("/workspaces");
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');
    await expect(page.locator(`text=${wsName}`).first()).toBeVisible({
      timeout: 10_000,
    });

    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await expect(filter).toBeVisible();
    const menu = page.locator('[data-testid="filter-suggestions"]');

    // Typing the (unique) name surfaces a typeahead suggestion for it.
    await filter.click();
    await filter.pressSequentially(wsName);
    await expect(menu).toBeVisible();
    await expect(menu.getByRole("option", { name: wsName })).toBeVisible();

    // Bare Enter with nothing highlighted must KEEP the free text — it must not
    // auto-apply the suggestion (the regression this guards: a single suggestion
    // under the cursor used to be applied on a plain Enter).
    await page.keyboard.press("Enter");
    await expect(filter).toHaveValue(wsName);
    await expect(menu).toBeHidden();

    // Reopen the menu by clearing + retyping (a bare Enter leaves the input
    // focused but closed, and refocusing an already-focused input fires no new
    // focus event). Arrow-highlight + Enter applies the suggestion — a
    // completed chip is the typed text plus a trailing space.
    await filter.fill("");
    await filter.pressSequentially(wsName);
    await expect(menu).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(filter).toHaveValue(`${wsName} `);

    // A mouse click on a suggestion applies it the same way.
    await filter.fill("");
    await filter.pressSequentially(wsName);
    await expect(menu).toBeVisible();
    await menu.getByRole("option", { name: wsName }).click();
    await expect(filter).toHaveValue(`${wsName} `);
  });

  test("the filter survives leaving the list and stays until cleared", async ({ page }) => {
    // sessionStorage is per tab, so this test owns its own state.
    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await page.goto("/workspaces");
    await expect(filter).toBeVisible();

    await filter.fill("status:errored ");
    await expect(filter).toHaveValue("status:errored ");

    // The stored form is the parsed terms, the same text the URL carries, so the
    // trailing space typed after a completed term is not restored.
    // Leave through the nav bar's bare /workspaces link, which carries no query.
    await page.goto("/settings/tokens");
    await page.getByRole("link", { name: "Workspaces", exact: true }).first().click();
    await page.waitForURL(/\/workspaces/);
    await expect(filter).toHaveValue("status:errored");

    // A reload keeps it too.
    await page.reload();
    await expect(filter).toHaveValue("status:errored");

    // It stays until it is explicitly cleared, and then it stays cleared.
    await filter.fill("");
    await page.goto("/settings/tokens");
    await page.getByRole("link", { name: "Workspaces", exact: true }).first().click();
    await page.waitForURL(/\/workspaces/);
    await expect(filter).toHaveValue("");
  });

  test("the stored filter is the normalised form, not the raw text", async ({ page }) => {
    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await page.goto("/workspaces");
    await filter.fill("env=prod ");

    await page.goto("/settings/tokens");
    await page.getByRole("link", { name: "Workspaces", exact: true }).first().click();
    await page.waitForURL(/\/workspaces/);
    await expect(filter).toHaveValue("env:prod");
  });

  test("a filter in the URL wins over the stored one", async ({ page }) => {
    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await page.goto("/workspaces");
    await filter.fill("status:errored ");
    await expect(filter).toHaveValue("status:errored ");

    // The same tab still holds "status:errored", so the two disagree here.
    await page.goto("/workspaces?q=status:applied");
    await expect(filter).toHaveValue("status:applied");
  });

  test("an old status: bookmark is rewritten once on load and keeps its meaning", async ({ page }) => {
    // Agent mode with no pool carries a `no_agent_pool` condition, so it is "unhealthy".
    const prefix = uniqueName("e2e-legacy");
    const token = getStoredToken();
    const lockedUnhealthy = `${prefix}-locked-bad`;
    const unlockedUnhealthy = `${prefix}-open-bad`;
    const lockedPlain = `${prefix}-locked-ok`;
    const bad = await createWorkspace(token, lockedUnhealthy, { "execution-mode": "agent" });
    await createWorkspace(token, unlockedUnhealthy, { "execution-mode": "agent" });
    const ok = await createWorkspace(token, lockedPlain);
    await lockWorkspace(token, bad);
    await lockWorkspace(token, ok);

    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await page.goto(`/workspaces?q=${encodeURIComponent(`${prefix} status:locked status:unhealthy`)}`);

    await expect(page.getByRole("link", { name: lockedUnhealthy })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole("link", { name: unlockedUnhealthy })).toBeHidden();
    await expect(page.getByRole("link", { name: lockedPlain })).toBeHidden();

    await expect(filter).toHaveValue(/locked:true/);
    await expect(filter).toHaveValue(/flag:no-agent-pool/);
    await expect(filter).not.toHaveValue(/status:/);
    await expect.poll(() => new URL(page.url()).searchParams.get("q") ?? "").not.toContain("status:");
    expect(new URL(page.url()).searchParams.get("q")).toContain("locked:true");
  });

  test("an old status: filter held in the tab is rewritten on load", async ({ page }) => {
    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await page.goto("/settings/tokens");
    await page.evaluate(() => sessionStorage.setItem("terrapod:workspace-filter", "status:locked"));
    await page.goto("/workspaces");
    await expect(filter).toHaveValue("locked:true");
    await expect
      .poll(() => page.evaluate(() => sessionStorage.getItem("terrapod:workspace-filter")))
      .toBe("locked:true");
  });

  // A refusal takes the not-ok branch; a body that is not JSON takes the catch. Neither may drop
  // an engine the URL asked for (a transport failure would too, but apiFetch retries those for seconds).
  for (const [name, reply] of [
    ["is refused", { status: 403, body: "{}" }],
    ["returns something that is not JSON", { status: 200, body: "not json" }],
  ] as const) {
    test(`an engine in the URL survives the engines request that ${name}`, async ({ page }) => {
      await page.route("**/api/v1/engines", (route) => route.fulfill(reply));
      const answered = page.waitForResponse((r) => r.url().includes("/api/v1/engines"));
      await page.goto("/workspaces?engine=terraform");
      await answered;
      // Give the page the chance to (wrongly) drop it, then check both places it lives.
      await page.waitForTimeout(600);
      expect(new URL(page.url()).searchParams.get("engine")).toBe("terraform");
      expect(await page.evaluate(() => sessionStorage.getItem("terrapod:workspace-engine"))).toBe("terraform");
    });
  }

  test("changing the grouping keeps the engine and the filter in the URL", async ({ page }) => {
    await page.goto("/workspaces?q=env%3Aprod&engine=terraform");
    await expect(page.locator('input[aria-label="Filter workspaces"]')).toHaveValue("env:prod");

    await page.getByRole("button", { name: "Group", exact: true }).click();
    await page.getByRole("menuitemradio", { name: "Repository", exact: true }).click();

    await expect.poll(() => new URL(page.url()).searchParams.get("group")).toBe("repo");
    const params = new URL(page.url()).searchParams;
    expect(params.get("engine")).toBe("terraform");
    expect(params.get("q")).toBe("env:prod");
  });

  test("a second tab does not inherit or overwrite the first tab's filter", async ({ page, context }) => {
    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await page.goto("/workspaces");
    await filter.fill("status:errored ");
    await expect(filter).toHaveValue("status:errored ");

    const other = await context.newPage();
    await other.goto("/workspaces");
    const otherFilter = other.locator('input[aria-label="Filter workspaces"]');
    await expect(otherFilter).toBeVisible();
    await expect(otherFilter).toHaveValue("");

    await otherFilter.fill("status:applied ");
    await page.reload();
    await expect(filter).toHaveValue("status:errored");
    await other.close();
  });

  test("the sort survives leaving the list and returning", async ({ page }) => {
    // Two workspaces under one prefix, so their order is the only thing that decides the assertion.
    const prefix = uniqueName("e2e-sort");
    const token = getStoredToken();
    await createWorkspace(token, `${prefix}-a`);
    await createWorkspace(token, `${prefix}-b`);

    const firstRow = () => page.locator("tbody tr", { hasText: prefix }).first();
    await page.goto("/workspaces");
    await page.locator('input[aria-label="Filter workspaces"]').fill(`${prefix} `);
    // Name sorts ascending by default, so "-a" comes first.
    await expect(firstRow()).toContainText(`${prefix}-a`);

    await page.getByRole("columnheader", { name: /Name/ }).first().click();
    await expect(firstRow()).toContainText(`${prefix}-b`);

    await page.goto("/settings/tokens");
    await page.getByRole("link", { name: "Workspaces", exact: true }).first().click();
    await page.waitForURL(/\/workspaces/);
    await page.locator('input[aria-label="Filter workspaces"]').fill(`${prefix} `);
    await expect(firstRow()).toContainText(`${prefix}-b`);
  });

  test("workspace settings can be updated", async ({ page }) => {
    // Create a workspace
    const wsName = `e2e-settings-${Date.now()}`;

    await page.goto("/workspaces");
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');

    // Navigate to workspace detail
    await page.click(`text=${wsName}`);

    // Click Edit on the overview tab
    await page.click('button:has-text("Edit")');

    // Auto-apply is a four-value mode select, not a boolean (#1274). Locate it
    // by its accessible name, NOT positionally: this test used to say
    // `input[type=checkbox].first()`, which silently re-aimed at the Terragrunt
    // checkbox when the control became a select, and kept passing while testing
    // nothing (#1280). A named locator fails closed instead.
    const autoApply = page.getByRole("combobox", { name: "Auto Apply" });
    await expect(autoApply).toHaveValue("never"); // default for a new workspace

    // Pick a conditional mode — the interesting case, since it has to survive
    // the round trip as a mode rather than collapsing back to a boolean.
    await autoApply.selectOption("create_update");

    // Save
    await page.click('button:has-text("Save")');

    // Wait for save to complete (Edit button re-appears)
    await expect(page.locator('button:has-text("Edit")')).toBeVisible({
      timeout: 10_000,
    });

    // The read view must name the mode, not a yes/no.
    await expect(
      page.getByText("create/update", { exact: true }),
    ).toBeVisible();

    // Reload proves it persisted server-side rather than living in React state.
    await page.reload();
    await page.click('button:has-text("Edit")');
    await expect(
      page.getByRole("combobox", { name: "Auto Apply" }),
    ).toHaveValue("create_update");
  });

  test("a refused settings save shows the API's reason, not a generic failure (#1586)", async ({
    page,
  }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("e2e-save-detail"));
    const detail =
      "Cannot change vcs-workflow while 7 PR run(s) are in flight. Cancel or discard them first.";

    // Refuse the save the way the API does (#282) — the exact body from the
    // report, both error shapes — without having to put real PR runs in flight.
    // Only the PATCH is stubbed; the page's reads go through.
    await page.route(`**/api/v1/workspaces/${wsId}`, (route) =>
      route.request().method() === 'PATCH'
        ? route.fulfill({
            status: 422,
            contentType: "application/vnd.api+json",
            body: JSON.stringify({
              errors: [{ status: "422", detail }],
              detail,
            }),
          })
        : route.continue(),
    );

    await page.goto(`/workspaces/${wsId}`);
    // Retry the click until the form opens: a click that lands before
    // hydration is otherwise lost.
    await expect(async () => {
      await page.locator('button:has-text("Edit")').first().click();
      await expect(page.locator('button:has-text("Save")').first()).toBeVisible(
        { timeout: 1_000 },
      );
    }).toPass({ timeout: 15_000 });
    await page.locator('button:has-text("Save")').first().click();

    await expect(page.getByText(detail)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("Failed to update workspace")).toHaveCount(0);
  });

  test("terragrunt toggle + version persists through settings (#534)", async ({
    page,
  }) => {
    // Enabling terragrunt reveals a version input; both must round-trip the
    // workspace PATCH and re-render on reload. Failure modes this catches:
    //   - terragrunt-enabled missing from the PATCH body
    //   - the version field not shown when enabled / not saved
    //   - the read view not reflecting the saved state
    // Name avoids the "terragrunt" substring so locators don't collide with it.
    const wsName = `e2e-tg-${Date.now()}`;

    await page.goto("/workspaces");
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');
    await page.click(`text=${wsName}`);

    // The Terragrunt setting lives in a <div> whose <dt> is exactly "Terragrunt";
    // scope all assertions to that block to avoid the auto-apply row.
    const tgBlock = page
      .getByText("Terragrunt", { exact: true })
      .locator("xpath=..");
    await expect(tgBlock).toBeVisible();
    await expect(tgBlock.getByText("Disabled")).toBeVisible(); // read view starts Disabled

    await page.click('button:has-text("Edit")');
    await tgBlock.getByRole("checkbox").check();

    // Version input appears only once enabled.
    const tgVersion = tgBlock.getByPlaceholder("e.g. 1.0");
    await expect(tgVersion).toBeVisible();
    await tgVersion.fill("1.0");

    await page.click('button:has-text("Save")');
    await expect(page.locator('button:has-text("Edit")')).toBeVisible({
      timeout: 10_000,
    });

    // Reload: the read view must show Enabled (v1.0).
    await page.reload();
    await expect(page.getByText("Enabled (v1.0)")).toBeVisible({
      timeout: 10_000,
    });
  });

  test("drift-ignore-rules editor adds, persists, and removes a rule", async ({
    page,
  }) => {
    // #482 — verify the workspace settings drift-ignore-rules editor
    // round-trips through the API. Failure cases the spec catches:
    //   - rule not added to the array on Enter / "Add" click
    //   - PATCH body missing the `drift-ignore-rules` attribute
    //   - re-rendered list doesn't reflect the saved value
    //   - Remove button doesn't drop the rule from state or PATCH
    const wsName = `e2e-drift-ignore-${Date.now()}`;
    const rule =
      "module.eks*.argocd_cluster.*.config.tls_client_config.ca_data";

    await page.goto("/workspaces");
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');
    await page.click(`text=${wsName}`);

    // Enter edit mode and add a rule via the Add button (covers both
    // the keydown handler and the click handler — Add reads the same
    // state, so a working click implies the typed input was captured).
    await page.click('button:has-text("Edit")');
    const ruleInput = page.locator('input[placeholder*="argocd_cluster"]');
    await expect(ruleInput).toBeVisible();
    await ruleInput.fill(rule);
    // Two Add buttons exist (trigger-prefixes + drift-ignore-rules) —
    // scope to the one inside the drift-ignore row.
    const driftAddRow = page
      .locator('input[placeholder*="argocd_cluster"]')
      .locator("..");
    await driftAddRow.locator('button:has-text("Add")').click();

    // The pill should render with the rule text.
    await expect(page.locator(`code:has-text("${rule}")`)).toBeVisible();

    // Save.
    await page.click('button:has-text("Save")');
    await expect(page.locator('button:has-text("Edit")')).toBeVisible({
      timeout: 10_000,
    });

    // Reload and confirm the value persisted server-side (the
    // settings round-trip is the part that broke for trigger_prefixes
    // in v0.35.x — this guards us against the same bug landing here).
    await page.reload();
    await expect(page.locator(`code:has-text("${rule}")`)).toBeVisible();

    // Remove the rule via Edit → Remove → Save, and confirm it's
    // gone after reload.
    await page.click('button:has-text("Edit")');
    const removeBtn = page
      .locator(`code:has-text("${rule}")`)
      .locator("..")
      .locator('button:has-text("Remove")');
    await removeBtn.click();
    await page.click('button:has-text("Save")');
    await expect(page.locator('button:has-text("Edit")')).toBeVisible({
      timeout: 10_000,
    });
    await page.reload();
    await expect(page.locator(`code:has-text("${rule}")`)).not.toBeVisible();
  });

  test("Health Issues indicator filters the list to unhealthy workspaces", async ({
    page,
  }) => {
    // Two workspaces via the API so the test is deterministic and self-isolated:
    //   - healthy: default local mode -> no health conditions
    //   - unhealthy: agent mode with no pool -> a `no_agent_pool` condition,
    //     the simplest one to provoke without running anything.
    const token = getStoredToken();
    const healthyName = uniqueName("e2e-health-ok");
    const unhealthyName = uniqueName("e2e-health-bad");
    await createWorkspace(token, healthyName);
    await createWorkspace(token, unhealthyName, { "execution-mode": "agent" });

    await page.goto("/workspaces");
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Both of MY workspaces are present before filtering. (Never assert global
    // counts/positions -- other workers mutate the same org concurrently.)
    const healthyRow = page.getByRole("link", { name: healthyName });
    const unhealthyRow = page.getByRole("link", { name: unhealthyName });
    await expect(unhealthyRow).toBeVisible({ timeout: 10_000 });
    await expect(healthyRow).toBeVisible();

    // The Health Issues chip inserts every condition as a `flag:` term, so the
    // filter bar shows what "unhealthy" means. The terms union, so the result
    // is any workspace with at least one condition.
    const healthBtn = page.getByRole("button", { name: /health issues/i });
    await expect(healthBtn).toBeVisible();
    await healthBtn.click();

    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await expect(filter).toHaveValue(/flag:no-agent-pool/);
    await expect(filter).toHaveValue(/flag:drifted/);
    await expect(filter).not.toHaveValue(/status:unhealthy/);
    await expect(unhealthyRow).toBeVisible();
    await expect(healthyRow).toBeHidden();

    // Toggling the chip again clears the whole set -- my healthy ws returns.
    await healthBtn.click();
    await expect(healthyRow).toBeVisible({ timeout: 10_000 });
  });

  test("the dropdowns union their own selections and typed terms intersect", async ({
    page,
  }) => {
    // Two workspaces in one name scope: one with a condition (no_agent_pool,
    // via agent mode with no pool), one with none.
    const prefix = uniqueName("e2e-or");
    const token = getStoredToken();
    const noPool = `${prefix}-nopool`;
    const plain = `${prefix}-plain`;
    await createWorkspace(token, noPool, { "execution-mode": "agent" });
    await createWorkspace(token, plain);

    await page.goto("/workspaces");
    const filter = page.locator('input[aria-label="Filter workspaces"]');
    const noPoolRow = page.getByRole("link", { name: noPool });
    const plainRow = page.getByRole("link", { name: plain });

    // Two flags: either. `drifted` matches neither workspace, `no-agent-pool`
    // matches one, so the union shows that one and not the other.
    await filter.fill(`${prefix} flag:drifted flag:no-agent-pool `);
    await expect(noPoolRow).toBeVisible({ timeout: 10_000 });
    await expect(plainRow).toBeHidden();

    // locked is its own key and still ANDs with a flag: nothing here is locked.
    await filter.fill(`${prefix} locked:true flag:no-agent-pool `);
    await expect(noPoolRow).toBeHidden();
    await expect(plainRow).toBeHidden();

    // locked:false ANDs the other way round.
    await filter.fill(`${prefix} locked:false flag:no-agent-pool `);
    await expect(noPoolRow).toBeVisible();
    await expect(plainRow).toBeHidden();
  });

  test("the Flags dropdown ticks several conditions and the Status badge counts only its own", async ({
    page,
  }) => {
    await page.goto("/workspaces");
    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await expect(filter).toBeVisible();

    await page.getByRole("button", { name: "Flags" }).click();
    await page.getByRole("menuitemcheckbox", { name: /Drifted/ }).click();
    await page.getByRole("menuitemcheckbox", { name: /VCS error/ }).click();
    await expect(filter).toHaveValue(/flag:drifted/);
    await expect(filter).toHaveValue(/flag:vcs-error/);
    await expect(page.getByTestId("ws-flags-count")).toHaveText("2");
    await expect(page.getByTestId("ws-status-count")).toHaveCount(0);
  });

  test("a typed term the dropdowns do not offer is not counted in either badge", async ({ page }) => {
    await createWorkspace(getStoredToken(), uniqueName("e2e-unoffered"));
    await page.goto("/workspaces");
    const filter = page.locator('input[aria-label="Filter workspaces"]');
    await expect(filter).toBeVisible();
    // The list has loaded, so the empty result below is the filter's doing.
    await expect(page.getByTestId("ws-row-select").first()).toBeVisible();

    // `flag:nope` and `status:locked` match nothing and have no row to tick.
    await filter.fill("flag:drifted flag:vcs-error flag:nope status:locked ");
    await expect(page.getByTestId("ws-row-select")).toHaveCount(0);
    await expect(page.getByTestId("ws-flags-count")).toHaveText("2");
    await expect(page.getByTestId("ws-status-count")).toHaveCount(0);
  });

  test("the Status dropdown counts a workspace by its run's status, not by its pill", async ({ page }) => {
    // A drifted workspace shows the drifted pill, but its run is still errored.
    const id = await createWorkspace(getStoredToken(), uniqueName("e2e-status-own"));
    await page.route("**/api/v1/workspaces?**", async (route) => {
      const res = await route.fetch();
      const body = await res.json();
      for (const w of body.data ?? []) {
        const mine = w.id === id;
        w.attributes["drift-status"] = mine ? "drifted" : "";
        w.attributes["latest-run"] = mine
          ? { id: "run-errored", status: "errored", "plan-only": false, "created-at": "2026-11-10T10:00:00Z" }
          : null;
      }
      await route.fulfill({ response: res, json: body });
    });

    try {
      await page.goto("/workspaces");
      await page.locator('button[aria-haspopup="menu"]', { hasText: "Status" }).click();
      const errored = page.getByRole("menuitemcheckbox", { name: /Errored/ });
      await expect(errored.locator("span").last()).toHaveText("1");
    } finally {
      await page.unrouteAll({ behavior: "ignoreErrors" });
    }
  });

  test("selecting workspaces offers bulk actions, and Plan runs on each one", async ({ page }) => {
    // Two workspaces under one prefix scope the list to this test's own rows.
    // `seedRun` uploads a configuration, which a workspace needs before it can
    // take a plan: the server refuses one without, and the result says so.
    const prefix = uniqueName("e2e-sel-plan");
    const token = getStoredToken();
    const a = await createWorkspace(token, `${prefix}-a`);
    const b = await createWorkspace(token, `${prefix}-b`);
    await seedRun(token, a, true);
    await seedRun(token, b, true);

    await listOnly(page, prefix, 2);

    // The standard view does not change until a box is ticked.
    await expect(page.getByTestId("ws-selection")).toHaveCount(0);

    // The header checkbox selects every visible row, and the Selection control
    // appears with the count.
    await page.getByTestId("ws-select-all").check();
    await expect(page.getByTestId("ws-selection")).toContainText("2");

    // Pressing it only opens a menu: it has no default action.
    await page.getByTestId("ws-selection").click();
    const menu = page.getByTestId("ws-selection-menu");
    await expect(menu).toBeVisible();
    await expect(menu.getByRole("menuitem")).toHaveCount(5);

    // Why an action is unavailable is in the row, not in a tooltip. Each
    // workspace has only a queued plan-only run, so Apply qualifies for neither.
    const apply = page.getByTestId("ws-selection-apply");
    await expect(apply).toContainText("0 of 2");
    await expect(apply).toHaveAttribute("aria-disabled", "true");

    // Plan qualifies for both, and runs on each, with no confirm on a precise pointer.
    await expect(page.getByTestId("ws-selection-plan")).toContainText("2 of 2");
    let dialogs = 0;
    page.on("dialog", (d) => {
      dialogs += 1;
      void d.dismiss();
    });
    await page.getByTestId("ws-selection-plan").click();
    await expect(page.getByTestId("ws-bulk-result")).toContainText("Plan: 2 of 2", { timeout: 15_000 });
    expect(dialogs, "Plan must not prompt on a precise pointer").toBe(0);
  });
  test("a refusal from the server is reported per workspace, not hidden", async ({ page }) => {
    // A workspace with no uploaded configuration refuses a plan. The batch must
    // say so for THAT workspace, with the server's own words, and not fail whole.
    const prefix = uniqueName("e2e-sel-refuse");
    const token = getStoredToken();
    await createWorkspace(token, `${prefix}-bare`);

    await listOnly(page, prefix, 1);
    await page.getByTestId("ws-row-select").check();
    await page.getByTestId("ws-selection").click();
    await page.getByTestId("ws-selection-plan").click();

    const result = page.getByTestId("ws-bulk-result");
    await expect(result).toContainText("Plan: 0 of 1", { timeout: 15_000 });
    await expect(result).toContainText(`${prefix}-bare`);
    await expect(result).toContainText("no uploaded configuration");
    // The refused workspace stays selected, so the reason beside it can be acted on.
    await expect(page.getByTestId("ws-row-select")).toBeChecked();
  });
  test("the header checkbox is also the clear, from a partial selection", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-clear");
    const token = getStoredToken();
    await createWorkspace(token, `${prefix}-a`);
    await createWorkspace(token, `${prefix}-b`);

    await listOnly(page, prefix, 2);

    await page.getByTestId("ws-row-select").first().check();
    await expect(page.getByTestId("ws-select-all")).toHaveJSProperty("indeterminate", true);

    // Clicking it from a partial selection clears, it does not complete it.
    await page.getByTestId("ws-select-all").click();
    await expect(page.getByTestId("ws-row-select").first()).not.toBeChecked();
    await expect(page.getByTestId("ws-selection")).toHaveCount(0);
  });
  test("an unavailable action is inert and says why, and never opens a confirm", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-inert");
    const token = getStoredToken();
    await createWorkspace(token, `${prefix}-a`);

    await listOnly(page, prefix, 1);
    await page.getByTestId("ws-row-select").check();
    await page.getByTestId("ws-selection").click();

    // No planned run, so Discard is off. It stays focusable (aria-disabled, not
    // `disabled`) so a screen reader reaches it and hears the reason beside it.
    const discard = page.getByTestId("ws-selection-discard");
    await expect(discard).toHaveAttribute("aria-disabled", "true");
    await expect(discard).toContainText("0 of 1");
    let dialogs = 0;
    page.on("dialog", (d) => {
      dialogs += 1;
      void d.dismiss();
    });
    // `force` skips Playwright's own "is it enabled" wait, which is the point:
    // a real user can still press it, and nothing must happen.
    await discard.click({ force: true });
    expect(dialogs, "an unavailable action must not open a confirm").toBe(0);
    await expect(page.getByTestId("ws-bulk-result")).toHaveCount(0);
  });
  test("Apply and Discard follow the server's answer, not the run's status", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-answer");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    // A planned run the server says can be neither applied nor discarded (no changes).
    await stubLatestRun(page, [id], () => ({ status: "planned", actions: NO_ACTIONS }));

    await listOnly(page, prefix, 1);
    await page.getByTestId("ws-row-select").check();
    await page.getByTestId("ws-selection").click();
    await expect(page.getByTestId("ws-selection-apply")).toContainText("0 of 1");
    await expect(page.getByTestId("ws-selection-discard")).toContainText("0 of 1");
    await page.unrouteAll({ behavior: "wait" });
  });

  test("Discard is offered on a run held after its plan, though its status is not planned", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-held");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    await stubLatestRun(page, [id], () => ({
      status: "planning",
      actions: { ...NO_ACTIONS, "is-discardable": true, "is-cancelable": true },
    }));

    await listOnly(page, prefix, 1);
    await page.getByTestId("ws-row-select").check();
    await page.getByTestId("ws-selection").click();
    await expect(page.getByTestId("ws-selection-discard")).toContainText("1 of 1");
    await expect(page.getByTestId("ws-selection-apply")).toContainText("0 of 1");
    await page.unrouteAll({ behavior: "wait" });
  });

  test("a latest run with no actions (an older API) offers only Plan", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-old");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    await stubLatestRun(page, [id], () => ({ status: "errored", actions: undefined }));

    await listOnly(page, prefix, 1);
    await page.getByTestId("ws-row-select").check();
    await page.getByTestId("ws-selection").click();
    await expect(page.getByTestId("ws-selection-plan")).toContainText("1 of 1");
    for (const action of ["retry", "cancel", "apply", "discard"]) {
      await expect(page.getByTestId(`ws-selection-${action}`)).toContainText("0 of 1");
    }
    await page.unrouteAll({ behavior: "wait" });
  });

  test("Plan is not offered on a workspace without plan/apply, and Retry follows the run's kind", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-ops");
    const ids = [
      await createWorkspace(getStoredToken(), `${prefix}-cfg`),
      await createWorkspace(getStoredToken(), `${prefix}-pa`),
    ];
    // Both are configure-only; the first's latest run is a configure, the second's a plan/apply.
    await stubLatestRun(
      page,
      ids,
      (i) => ({
        status: "errored",
        "run-kind": i === 0 ? "configure" : "plan_apply",
        actions: { ...NO_ACTIONS, "is-retryable": true },
      }),
      { "plan-apply-enabled": false, "configure-enabled": true },
    );

    await listOnly(page, prefix, 2);
    await page.getByTestId("ws-select-all").check();
    await page.getByTestId("ws-selection").click();
    await expect(page.getByTestId("ws-selection-plan")).toContainText("0 of 2");
    await expect(page.getByTestId("ws-selection-retry")).toContainText("1 of 2");
    await page.unrouteAll({ behavior: "wait" });
  });
  test("Apply and Discard confirm in the irreversible tier, with the count and the names, and send nothing if declined", async ({ page }) => {
    // A planned, apply-capable run is the state both need, and reaching it for
    // real takes a runner. So the LIST response is stubbed to carry one, and the
    // calls it would make are recorded; the confirm and the eligibility are real.
    const prefix = uniqueName("e2e-sel-confirm");
    const token = getStoredToken();
    const ids = [
      await createWorkspace(token, `${prefix}-a`),
      await createWorkspace(token, `${prefix}-b`),
    ];

    const posts: string[] = [];
    await stubLatestRun(page, ids, (i) => ({
      id: `run-planned-${i}`,
      status: "planned",
      actions: { ...NO_ACTIONS, "is-confirmable": true, "is-discardable": true },
    }));
    await page.route("**/api/v1/runs/*/actions/*", async (route) => {
      posts.push(route.request().url());
      await route.fulfill({ status: 200, contentType: "application/vnd.api+json", body: "{}" });
    });

    await listOnly(page, prefix, 2);
    await page.getByTestId("ws-select-all").check();
    await page.getByTestId("ws-selection").click();

    // Both qualify, so Apply and Discard are on and say so in the row.
    await expect(page.getByTestId("ws-selection-apply")).toContainText("2 of 2");
    await expect(page.getByTestId("ws-selection-discard")).toContainText("2 of 2");

    // Declining sends nothing.
    const seen: string[] = [];
    page.once("dialog", (d) => {
      seen.push(d.message());
      void d.dismiss();
    });
    await page.getByTestId("ws-selection-apply").click();
    await expect.poll(() => seen.length).toBe(1);
    expect(seen[0]).toContain(`${prefix}-a`);
    expect(seen[0]).toContain(`${prefix}-b`);
    expect(seen[0]).toContain("(2)");
    expect(posts, "declining the confirm must send nothing").toEqual([]);

    // Enter on an item hands focus back to the trigger before the confirm opens.
    const trigger = page.getByTestId("ws-selection");
    await trigger.click();
    await page.getByTestId("ws-selection-apply").focus();
    page.once("dialog", (d) => void d.dismiss());
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("ws-selection-menu")).toHaveCount(0);
    await expect(trigger).toBeFocused();

    // Accepting sends one call per workspace, to the existing endpoint.
    page.once("dialog", (d) => void d.accept());
    await page.getByTestId("ws-selection").click();
    await page.getByTestId("ws-selection-discard").click();
    await expect(page.getByTestId("ws-bulk-result")).toContainText("Discard: 2 of 2", { timeout: 15_000 });
    expect(posts.filter((u) => u.endsWith("/actions/discard"))).toHaveLength(2);

    // The page reloads the list after an action (and on SSE), and each reload runs
    // the stubbed `route.fetch()` above, so one is always in flight when the test
    // ends. `ignoreErrors` is what drops whatever that handler then throws; it is
    // NOT interchangeable with `wait`, which this line used to pass and which is
    // why the spec failed intermittently on main with "Route is already handled!"
    // reported against the `fulfill` above. Measured against playwright 1.58:
    // `wait` awaits the in-flight handlers and still PROPAGATES an exception from
    // one, failing the test; `ignoreErrors` sets the flag that swallows it (see
    // `RouteHandler.stop` in playwright-core). Every assertion has already run by
    // here, so a stub failing during teardown must not fail the test — which is
    // what playwright's own message recommends, and what log-reserve.spec.ts does.
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  // One selected row, with where Tab and Shift+Tab lead from the closed trigger
  // marked, and a trail of every element that takes focus from here on.
  async function selectionKeyboard(page: Page) {
    const prefix = uniqueName("e2e-sel-keys");
    await createWorkspace(getStoredToken(), `${prefix}-a`);
    await listOnly(page, prefix, 1);
    await page.getByTestId("ws-row-select").check();
    const trigger = page.getByTestId("ws-selection");
    const mark = async (key: string, attr: string) => {
      await trigger.focus();
      await page.keyboard.press(key);
      const body = await page.evaluate((a) => {
        const el = document.activeElement;
        el?.setAttribute(a, "1");
        return el === document.body;
      }, attr);
      expect(body, `${key} from the closed trigger must land on an element`).toBe(false);
    };
    await mark("Tab", "data-e2e-after");
    await mark("Shift+Tab", "data-e2e-before");
    await page.evaluate(() => {
      const w = window as unknown as { __trail: string[] };
      w.__trail = [];
      document.addEventListener("focusin", (e) => {
        const el = e.target as HTMLElement;
        w.__trail.push(
          el.hasAttribute("data-e2e-after") ? "after" : el.hasAttribute("data-e2e-before") ? "before" : (el.dataset.testid ?? el.tagName),
        );
      });
    });
    await trigger.click();
    await expect(page.getByTestId("ws-selection-plan")).toBeFocused();
    await page.evaluate(() => ((window as unknown as { __trail: string[] }).__trail = []));
    return () => page.evaluate(() => (window as unknown as { __trail: string[] }).__trail);
  }

  test("Tab leaves the Selection menu through the trigger, to the next control", async ({ page }) => {
    const trail = await selectionKeyboard(page);
    await page.keyboard.press("Tab");
    await expect(page.getByTestId("ws-selection-menu")).toHaveCount(0);
    await expect(page.locator("[data-e2e-after]")).toBeFocused();
    // Focus passes through the trigger, so it never drops to the body.
    expect(await trail()).toEqual(["ws-selection", "after"]);
  });

  test("Shift+Tab leaves the Selection menu through the trigger, to the previous control", async ({ page }) => {
    const trail = await selectionKeyboard(page);
    await page.keyboard.press("Shift+Tab");
    await expect(page.getByTestId("ws-selection-menu")).toHaveCount(0);
    await expect(page.locator("[data-e2e-before]")).toBeFocused();
    expect(await trail()).toEqual(["ws-selection", "before"]);
  });

  test("Home and End move to the first and last item of the Selection menu", async ({ page }) => {
    await selectionKeyboard(page);
    await page.keyboard.press("End");
    await expect(page.getByTestId("ws-selection-discard")).toBeFocused();
    await page.keyboard.press("Home");
    await expect(page.getByTestId("ws-selection-plan")).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(page.getByTestId("ws-selection-retry")).toBeFocused();
    await page.keyboard.press("End");
    await expect(page.getByTestId("ws-selection-discard")).toBeFocused();
    // Escape and the arrows are unchanged: it still closes and returns to the trigger.
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("ws-selection-menu")).toHaveCount(0);
    await expect(page.getByTestId("ws-selection")).toBeFocused();
  });

  test("on a precise pointer Plan and Cancel open no dialog, and Retry of an apply-capable run does", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-tiers");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    const posts = await stubBulkRuns(page, [id]);

    await listOnly(page, prefix, 1);
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(false);

    const dialogs: string[] = [];
    page.on("dialog", (d) => {
      dialogs.push(d.message());
      void d.dismiss();
    });

    await runFromMenu(page, "plan");
    await expect(page.getByTestId("ws-bulk-result")).toContainText("Plan: 1 of 1", { timeout: 15_000 });
    await runFromMenu(page, "cancel");
    await expect(page.getByTestId("ws-bulk-result")).toContainText("Cancel run: 1 of 1", { timeout: 15_000 });
    expect(posts).toHaveLength(2);
    expect(dialogs, "Plan and Cancel must not prompt on a precise pointer").toEqual([]);

    // This run can apply, so its Retry asks on every pointer.
    await runFromMenu(page, "retry");
    await expect.poll(() => dialogs.length).toBe(1);
    // Requests reach the route in order, so once this one is recorded any retry sent before it is too.
    await page.evaluate(() => fetch("/api/v1/runs/settle/actions/settle", { method: "POST" }));
    await expect.poll(() => posts.some((u) => u.endsWith("/actions/settle"))).toBe(true);
    expect(posts.some((u) => u.endsWith("/actions/retry"))).toBe(false);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("on a precise pointer Retry of a plan-only run opens no dialog", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-retry-plan");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    const posts = await stubBulkRuns(page, [id], () => true);

    await listOnly(page, prefix, 1);
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(false);
    let dialogs = 0;
    page.on("dialog", (d) => {
      dialogs += 1;
      void d.dismiss();
    });

    await runFromMenu(page, "retry");
    await expect(page.getByTestId("ws-bulk-result")).toContainText("Retry run: 1 of 1", { timeout: 15_000 });
    expect(posts.filter((u) => u.endsWith("/actions/retry"))).toHaveLength(1);
    expect(dialogs, "a plan-only retry applies nothing, so a mouse click does not ask").toBe(0);
  });

  test("one apply-capable run among plan-only ones makes Retry ask on every pointer", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-retry-mixed");
    const token = getStoredToken();
    const ids = [await createWorkspace(token, `${prefix}-a`), await createWorkspace(token, `${prefix}-b`)];
    const posts = await stubBulkRuns(page, ids, (i) => i === 0);

    await listOnly(page, prefix, 2);
    await page.getByTestId("ws-select-all").check();
    const seen: string[] = [];
    page.once("dialog", (d) => {
      seen.push(d.message());
      void d.dismiss();
    });
    await page.getByTestId("ws-selection").click();
    await page.getByTestId("ws-selection-retry").click();
    await expect.poll(() => seen.length).toBe(1);
    expect(seen[0]).toContain("(2)");
    // Requests reach the route in order, so once this one is recorded any retry sent before it is too.
    await page.evaluate(() => fetch("/api/v1/runs/settle/actions/settle", { method: "POST" }));
    await expect.poll(() => posts.some((u) => u.endsWith("/actions/settle"))).toBe(true);
    expect(posts.some((u) => u.endsWith("/actions/retry"))).toBe(false);
  });

  test("on a precise pointer Retry of a plan-only destroy asks", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-retry-destroy");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    const posts = await stubBulkRuns(page, [id], () => true, () => true);

    await listOnly(page, prefix, 1);
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(false);
    const seen: string[] = [];
    page.once("dialog", (d) => {
      seen.push(d.message());
      void d.dismiss();
    });
    await runFromMenu(page, "retry");
    await expect.poll(() => seen.length).toBe(1);
    // Requests reach the route in order, so once this one is recorded any retry sent before it is too.
    await page.evaluate(() => fetch("/api/v1/runs/settle/actions/settle", { method: "POST" }));
    await expect.poll(() => posts.some((u) => u.endsWith("/actions/settle"))).toBe(true);
    expect(posts.some((u) => u.endsWith("/actions/retry"))).toBe(false);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("a failure with no server message is worded by the page, not by the library", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-fail");
    const ids = [await createWorkspace(getStoredToken(), `${prefix}-a`), await createWorkspace(getStoredToken(), `${prefix}-b`)];
    await stubLatestRun(page, ids, (i) => ({
      id: `run-stub-${i}`,
      status: "errored",
      actions: { ...NO_ACTIONS, "is-retryable": true },
    }));
    // The first workspace is unreachable, the second answers 500 with no body.
    await page.route("**/api/v1/runs/*/actions/retry", (route) =>
      route.request().url().includes("run-stub-0") ? route.abort() : route.fulfill({ status: 500, body: "" }),
    );

    await listOnly(page, prefix, 2);
    await page.getByTestId("ws-select-all").check();
    page.once("dialog", (d) => void d.accept());
    await page.getByTestId("ws-selection").click();
    await page.getByTestId("ws-selection-retry").click();

    const result = page.getByTestId("ws-bulk-result");
    await expect(result).toContainText("Retry run: 0 of 2", { timeout: 15_000 });
    await expect(result).toContainText(`${prefix}-a: Could not reach the server`);
    await expect(result).toContainText(`${prefix}-b: Request failed (500)`);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  /**
   * The page's own read of `configure-enabled`, which only an e2e can reach.
   *
   * `workspace-bulk-actions.ts` is pinned at the unit tier -- two named tests
   * fail if either `w.configureEnabled` is hardcoded there. But the PLUMBING
   * that fills it, `page.tsx`'s `configureEnabled: w.attributes['configure-enabled']
   * === true`, is in a `.tsx`, and `web/tests` runs under bare `node --test`
   * with no transpiler, so nothing there can import it. Hardcoding the flag at
   * THAT line passed every test: no existing row is configure-shaped, because
   * `stubLatestRun` defaults a run to `plan_apply` and the retry rule only
   * consults the flag on a configure or composite run (#2621).
   *
   * So the pair below is the gate, and it needs both halves: the refusal alone
   * passes if the flag is hardcoded false, and the eligibility alone passes if
   * it is hardcoded true.
   */
  const compositeRetryRow = async (page: Page, prefix: string, configureEnabled: boolean) => {
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    await stubLatestRun(
      page,
      [id],
      () => ({
        status: "errored",
        // A composite: an apply followed by a configure. This is the shape whose
        // Retry rule reads `configureEnabled` at all.
        "run-kind": "full",
        actions: { ...NO_ACTIONS, "is-retryable": true },
      }),
      { "configure-enabled": configureEnabled },
    );
    await listOnly(page, prefix, 1);
    await page.getByTestId("ws-row-select").check();
    await page.getByTestId("ws-selection").click();
    return page.getByTestId("ws-selection-retry");
  };

  test("Retry refuses a combined run on a workspace with configure disabled", async ({ page }) => {
    const retry = await compositeRetryRow(page, uniqueName("e2e-sel-cfg-off"), false);
    await expect(retry).toHaveAttribute("aria-disabled", "true");
    await expect(retry).toContainText("0 of 1 eligible");
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("and offers the same run once configure is enabled", async ({ page }) => {
    const retry = await compositeRetryRow(page, uniqueName("e2e-sel-cfg-on"), true);
    await expect(retry).toHaveAttribute("aria-disabled", "false");
    await expect(retry).toContainText("1 of 1 eligible");
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  /**
   * The selection is frozen while it is the input to something already running.
   *
   * The boxes used to stay live, and the completion handler filters the
   * selection down to the workspaces that FAILED -- so a row ticked mid-batch
   * was in the selection, absent from the results, and silently cleared: never
   * acted on, never reported, and gone from the screen as if it had been
   * (#2621). Freezing them removes the state rather than papering over it, and
   * then the filter is correct by construction because nothing can be added.
   */
  test("row and header checkboxes are off while a bulk action is in flight", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-frozen");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    await stubLatestRun(page, [id], () => ({
      status: "errored",
      actions: { ...NO_ACTIONS, "is-retryable": true },
    }));
    // Hold the action open so the in-flight state can be observed at all.
    let release: () => void = () => {};
    const held = new Promise<void>((r) => {
      release = r;
    });
    await page.route("**/api/v1/runs/*/actions/retry", async (route) => {
      await held;
      await route.fulfill({ status: 200, contentType: "application/vnd.api+json", body: "{}" });
    });

    await listOnly(page, prefix, 1);
    const row = page.getByTestId("ws-row-select");
    const all = page.getByTestId("ws-select-all");
    await expect(row).toBeEnabled();
    await row.check();
    page.once("dialog", (d) => void d.accept());
    await page.getByTestId("ws-selection").click();
    await page.getByTestId("ws-selection-retry").click();

    await expect(row).toBeDisabled();
    await expect(all).toBeDisabled();
    release();
    await expect(page.getByTestId("ws-bulk-result")).toContainText("Retry run: 1 of 1", { timeout: 15_000 });
    // And back on afterwards: a frozen selection that never thaws is worse.
    await expect(row).toBeEnabled();
    await expect(all).toBeEnabled();
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });
});

test.describe("Workspaces: bulk confirmations at tablet width with a touch pointer", () => {
  test.use({ viewport: { width: 1024, height: 768 }, hasTouch: true, isMobile: true });

  test("Plan, Retry and Cancel each ask first, and send nothing when declined", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-touch");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    const posts = await stubBulkRuns(page, [id]);

    await listOnly(page, prefix, 1);
    // A pointer that is not coarse would let these pass without a dialog.
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);

    for (const action of ["plan", "retry", "cancel"]) {
      const seen: string[] = [];
      page.once("dialog", (d) => {
        seen.push(d.message());
        void d.dismiss();
      });
      await runFromMenu(page, action);
      await expect.poll(() => seen.length, { message: action }).toBe(1);
      expect(seen[0]).toContain("(1)");
      expect(seen[0]).toContain(`${prefix}-a`);
    }
    expect(posts, "declining each confirm must send nothing").toEqual([]);

    page.once("dialog", (d) => void d.accept());
    await runFromMenu(page, "plan");
    await expect(page.getByTestId("ws-bulk-result")).toContainText("Plan: 1 of 1", { timeout: 15_000 });
    expect(posts).toHaveLength(1);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });
});

test.describe("Workspaces: Retry of a plan-only run on a touch pointer", () => {
  test.use({ viewport: { width: 1024, height: 768 }, hasTouch: true, isMobile: true });

  test("asks first, because touch confirms every single-tap action", async ({ page }) => {
    const prefix = uniqueName("e2e-sel-touch-plan");
    const id = await createWorkspace(getStoredToken(), `${prefix}-a`);
    const posts = await stubBulkRuns(page, [id], () => true);

    await listOnly(page, prefix, 1);
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
    const seen: string[] = [];
    page.once("dialog", (d) => {
      seen.push(d.message());
      void d.dismiss();
    });
    await runFromMenu(page, "retry");
    await expect.poll(() => seen.length).toBe(1);
    expect(seen[0]).toContain(`${prefix}-a`);
    // Requests reach the route in order, so once this one is recorded any retry sent before it is too.
    await page.evaluate(() => fetch("/api/v1/runs/settle/actions/settle", { method: "POST" }));
    await expect.poll(() => posts.some((u) => u.endsWith("/actions/settle"))).toBe(true);
    expect(posts.some((u) => u.endsWith("/actions/retry")), "declining must send nothing").toBe(false);
  });
});

test.describe("Workspace access tab (#1456)", () => {
  test("shows who can reach the workspace, including the role-free paths", async ({
    page,
  }) => {
    // From the stored auth state, not page.evaluate: before the first goto
    // the page is about:blank, where reading localStorage is a SecurityError.
    const token = getStoredToken();
    const name = `acctab${Date.now()}`;
    const res = await page.request.post(
      "/api/v2/organizations/default/workspaces",
      {
        headers: {
          "Content-Type": "application/vnd.api+json",
          Authorization: `Bearer ${token}`,
        },
        data: {
          data: {
            type: "workspaces",
            attributes: { name, labels: { env: "prod" } },
          },
        },
      },
    );
    expect(res.status()).toBe(201);
    const wsId = (await res.json()).data.id;

    await page.goto(`/workspaces/${wsId}?tab=access`);
    const panel = page.getByTestId("resource-access");
    await expect(panel).toBeVisible({ timeout: 15_000 });

    // The platform paths must be present: a role list alone reads as the
    // complete answer when a platform admin reaches everything anyway.
    await expect(panel.getByText(/platform admins/i)).toBeVisible({
      timeout: 15_000,
    });
  });
});

test.describe("Workspace security scanning settings (#1763)", () => {
  test("an operator can see and change enforcement from the UI", async ({
    page,
  }) => {
    // Before #1763 these were managed through the API and the Terraform
    // provider only, so an operator with the UI in front of them could not
    // see what the workspace was set to, let alone change it.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("scanui"));

    await page.goto(`/workspaces/${wsId}`);

    const enforcement = page.getByLabel("Enforcement", { exact: true });
    await expect(enforcement).toBeVisible({ timeout: 15_000 });
    // The workspace default, shown rather than assumed.
    await expect(enforcement).toHaveValue("advisory");

    await enforcement.selectOption("enforced");

    // The change round-trips through the API, so it survives a reload —
    // a select that only moved in the browser would pass a naive assertion.
    await expect(async () => {
      await page.reload();
      await expect(page.getByLabel("Enforcement", { exact: true })).toHaveValue(
        "enforced",
      );
    }).toPass({ timeout: 20_000 });

    const res = await page.request.get(`/api/v1/workspaces/${wsId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status()).toBe(200);
    expect(
      (await res.json()).data.attributes["security-scan-enforcement"],
    ).toBe("enforced");
  });

  test("ignored rules round-trip as a list", async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("scanskip"));

    await page.goto(`/workspaces/${wsId}`);
    const skip = page.getByLabel("Ignored rules");
    await expect(skip).toBeVisible({ timeout: 15_000 });

    await skip.fill("CKV_AWS_24, CKV2_AWS_5");
    await skip.blur();

    await expect(async () => {
      const res = await page.request.get(`/api/v1/workspaces/${wsId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(
        (await res.json()).data.attributes["security-scan-skip-rules"],
      ).toEqual(["CKV_AWS_24", "CKV2_AWS_5"]);
    }).toPass({ timeout: 20_000 });
  });
});

test.describe("Workspace runner debug mode (#1764)", () => {
  test("the toggle round-trips and the banner says it is on", async ({
    page,
  }) => {
    // The indicator is the half that is easy to drop: a setting that only
    // shows on the tab that sets it leaves an operator on any other tab
    // unaware that failed pods are being held with this workspace's
    // credentials in them.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("debugui"));

    await page.goto(`/workspaces/${wsId}`);

    const toggle = page.getByLabel("Debug mode", { exact: true });
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    await expect(toggle).not.toBeChecked();
    // Off means no banner, not a banner saying "off".
    await expect(page.getByTestId("debug-mode-banner")).toHaveCount(0);

    // `.click()`, never `.check()`: the toggle is a controlled input whose
    // checked state comes from the fetched workspace, so it only flips once the
    // PATCH resolves. `.check()` asserts the state changed synchronously and
    // throws "Clicking the checkbox did not change its state". The
    // `toBeChecked()` below is the wait.
    await toggle.click();
    await expect(toggle).toBeChecked({ timeout: 15_000 });

    await expect(page.getByTestId("debug-mode-banner")).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByTestId("debug-mode-indicator")).toBeVisible();

    // Survives a reload, so this is the stored value and not browser state.
    await expect(async () => {
      await page.reload();
      await expect(
        page.getByLabel("Debug mode", { exact: true }),
      ).toBeChecked();
      await expect(page.getByTestId("debug-mode-banner")).toBeVisible();
    }).toPass({ timeout: 20_000 });

    const res = await page.request.get(`/api/v1/workspaces/${wsId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status()).toBe(200);
    expect((await res.json()).data.attributes["debug-mode"]).toBe(true);
  });

  test("turning it back off clears the banner", async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("debugoff"));

    await page.goto(`/workspaces/${wsId}`);
    const toggle = page.getByLabel("Debug mode", { exact: true });
    await expect(toggle).toBeVisible({ timeout: 15_000 });

    await toggle.click();
    await expect(toggle).toBeChecked({ timeout: 15_000 });
    await expect(page.getByTestId("debug-mode-banner")).toBeVisible({
      timeout: 15_000,
    });

    await toggle.click();
    await expect(toggle).not.toBeChecked({ timeout: 15_000 });
    await expect(page.getByTestId("debug-mode-banner")).toHaveCount(0);

    await expect(async () => {
      const res = await page.request.get(`/api/v1/workspaces/${wsId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect((await res.json()).data.attributes["debug-mode"]).toBe(false);
    }).toPass({ timeout: 20_000 });
  });
});

test.describe("AI policy gate (#1766)", () => {
  // The E2E stack runs with `ai_summary.enabled = false`, so the gate is off
  // and there is no verdict to assert on — the positive paths need a real
  // model and belong to the live Tilt smoke. What IS load-bearing here, and
  // what a mocked test could not show, is the surface parity #1763 exists to
  // enforce: the per-workspace override reaching the GUI, and the gate-off
  // path staying quiet instead of rendering an empty panel on every run.
  test("the per-workspace override round-trips through the GUI", async ({
    page,
  }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("aipolui"));

    await page.goto(`/workspaces/${wsId}`);

    const select = page.getByTestId("ai-policy-mode");
    await expect(select).toBeVisible({ timeout: 15_000 });
    await expect(select).toHaveValue("default");

    await select.selectOption("disabled");
    // The value comes from the fetched workspace, so it settles only once the
    // PATCH resolves — this assertion is the wait, not a redundant check.
    await expect(select).toHaveValue("disabled", { timeout: 15_000 });

    // Survives a reload, so this is the stored value and not browser state.
    await expect(async () => {
      await page.reload();
      await expect(page.getByTestId("ai-policy-mode")).toHaveValue("disabled");
    }).toPass({ timeout: 20_000 });

    const res = await page.request.get(`/api/v1/workspaces/${wsId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status()).toBe(200);
    expect((await res.json()).data.attributes["ai-policy-mode"]).toBe(
      "disabled",
    );
  });

  test("the gate being off is reported, not a 500", async ({ page }) => {
    // A run that does not exist must 404. The endpoint is the one an operator
    // (and the MCP tool) reads to find out WHY a run is held, so it failing
    // loudly is worse than the gate being off.
    const token = getStoredToken();
    const res = await page.request.get(
      "/api/terrapod/v1/runs/run-00000000-0000-0000-0000-000000000000/ai-policy",
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(res.status()).toBe(404);
  });

  test("no AI policy panel leaks onto a workspace with the gate off", async ({
    page,
  }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("aipoloff"));

    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByTestId("ai-policy-mode")).toBeVisible({
      timeout: 15_000,
    });

    // The panel is a RUN surface. Its heading must not appear on the workspace
    // page, and no half-rendered verdict box either.
    await expect(page.locator("text=AI Policy Gate")).toHaveCount(0);
    await expect(
      page.locator("text=Waiting for the AI policy verdict"),
    ).toHaveCount(0);
  });
});

// The workspace reads below use `/api/v1`, the canonical native surface. They
// arrived from the 1.9 line on `/api/v2`, where that was correct because the bare
// workspace resource had no native GET or PATCH at all. Here it does, and `/api/v2`
// is a deprecated alias (see `docs/deprecations.md`), so leaving them on it would
// have them break at its removal rather than now. `web/tests/engine-scoped-prefixes.test.ts`
// does not catch this: it scans `web/`, not `e2e/`.
//
// The prefix is still per-ROUTE and not guessable from a neighbour —
// `services/tests/api/api_route_contract.json` is the authority, and a wrong one
// is a silent 404 no contract gate can see.
test.describe("Fork pull request plans (GHSA-gp5w-76rw-c452)", () => {
  test("a new workspace defaults to off and the toggle round-trips", async ({
    page,
  }) => {
    // Defaulting off is the whole point, so the default is asserted first:
    // a control that merely persists whatever it is set to would pass every
    // other assertion here while leaving fork pull requests planning.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("forkplans"));

    await page.goto(`/workspaces/${wsId}`);

    const toggle = page.getByLabel("Plan fork pull requests", { exact: true });
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    await expect(toggle).not.toBeChecked();
    await expect(page.getByTestId("allow-fork-pr-plans-indicator")).toHaveCount(
      0,
    );

    // `.click()`, never `.check()` — the input is controlled by the fetched
    // workspace, so it flips only once the PATCH resolves.
    await toggle.click();
    await expect(toggle).toBeChecked({ timeout: 15_000 });
    await expect(
      page.getByTestId("allow-fork-pr-plans-indicator"),
    ).toBeVisible();

    await expect(async () => {
      await page.reload();
      await expect(
        page.getByLabel("Plan fork pull requests", { exact: true }),
      ).toBeChecked();
    }).toPass({ timeout: 20_000 });

    const res = await page.request.get(`/api/v1/workspaces/${wsId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status()).toBe(200);
    expect((await res.json()).data.attributes["allow-fork-pr-plans"]).toBe(
      true,
    );
  });

  test("turning it back off persists", async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName("forkoff"));

    await page.goto(`/workspaces/${wsId}`);
    const toggle = page.getByLabel("Plan fork pull requests", { exact: true });
    await expect(toggle).toBeVisible({ timeout: 15_000 });

    await toggle.click();
    await expect(toggle).toBeChecked({ timeout: 15_000 });
    await toggle.click();
    await expect(toggle).not.toBeChecked({ timeout: 15_000 });

    // The off direction is the one a naive `if value` guard drops, leaving the
    // operator looking at an unchecked box over a workspace that still plans.
    await expect(async () => {
      const res = await page.request.get(`/api/v1/workspaces/${wsId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect((await res.json()).data.attributes["allow-fork-pr-plans"]).toBe(
        false,
      );
    }).toPass({ timeout: 20_000 });
  });
});
