import { expect, type Page } from "@playwright/test";

// Scope the list to one test's own rows: on a busy list the table can keep rows the
// filter excludes. A reload clears it, so retry from a fresh page.
export async function listOnly(page: Page, prefix: string, count: number) {
  for (let attempt = 1; ; attempt++) {
    await page.goto("/workspaces");
    await page.locator('input[aria-label="Filter workspaces"]').fill(`${prefix} `);
    try {
      await expect(page.getByTestId("ws-row-select")).toHaveCount(count, { timeout: 4_000 });
      return;
    } catch (err) {
      if (attempt >= 4) throw err;
    }
  }
}

// Stubs the list so each workspace in `ids` carries the latest run `run(i)` and the extra attributes.
export async function stubLatestRun(
  page: Page,
  ids: string[],
  run: (i: number) => Record<string, unknown> | null,
  attrs: Record<string, unknown> = {},
) {
  await page.route("**/api/v1/workspaces?**", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    for (const w of body.data ?? []) {
      const i = ids.indexOf(w.id);
      if (i < 0) continue;
      const latest = run(i);
      w.attributes["latest-run"] = latest && {
        id: `run-stub-${i}`,
        "plan-only": false,
        "created-at": "2026-11-10T10:00:00Z",
        "run-kind": "plan_apply",
        ...latest,
      };
      Object.assign(w.attributes, attrs);
    }
    await route.fulfill({ response: res, json: body });
  });
}

export const NO_ACTIONS = {
  "is-confirmable": false,
  "is-discardable": false,
  "is-cancelable": false,
  "is-retryable": false,
};
