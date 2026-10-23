// The per-host status map, checked against the SERVER that produces the
// statuses and against the catalogue that renders them (#1928).
//
// Both halves are silent when they break. The server derives a host's status
// (`_host_result_json` in `api/routers/runs.py`) and this map translates it, so
// a status the server adds and the map does not know renders as a raw
// lower-case slug in the middle of a translated table — and a key the map names
// and `en.json` does not renders as `MISSING_KEY`. Neither is visible to
// `i18n:check` (key parity) or to `i18n:resolve` (which can only read a literal
// key), which is why this exists at all. Same reasoning as
// `variable-category.test.ts`.
//
// The server's status set is DERIVED from its own source rather than listed
// here: a list would be a record of what the statuses were when this was
// written, which is exactly the drift it is meant to catch.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  HOST_STATUS_KEY,
  hostStatusBadge,
  hostStatusKey,
} from "../src/lib/host-status.ts";

const en = JSON.parse(readFileSync("messages/en.json", "utf8"));
const HOSTS = en.runDetail.hosts;

/** Every status literal the server's own derivation can return. */
function serverStatuses(): string[] {
  const source = readFileSync(
    "../services/terrapod/api/routers/runs.py",
    "utf8",
  );
  // The derivation and the not-reached row, both inside `# ── Per-host results`.
  const start = source.indexOf("def _host_result_json");
  const end = source.indexOf("async def _targeted_hosts");
  assert.ok(
    start > 0 && end > start,
    "could not locate the host-result serialisers",
  );
  const block = source.slice(start, end);
  const found = new Set<string>();
  for (const m of block.matchAll(/status\s*=\s*"([a-z-]+)"/g)) found.add(m[1]);
  for (const m of block.matchAll(/"status":\s*"([a-z-]+)"/g)) found.add(m[1]);
  return [...found].sort();
}

test("every status the server can emit has a message key", () => {
  const statuses = serverStatuses();
  assert.ok(
    statuses.length >= 6,
    `only found ${statuses.length} statuses in runs.py (${statuses}) — the matcher ` +
      "stopped matching rather than the statuses going away",
  );
  const unmapped = statuses.filter((s) => !hostStatusKey(s));
  assert.deepEqual(
    unmapped,
    [],
    `the server emits these and the UI would render them as raw slugs: ${unmapped}`,
  );
});

test("the map names no status the server cannot emit", () => {
  const statuses = new Set(serverStatuses());
  const extra = Object.keys(HOST_STATUS_KEY).filter((s) => !statuses.has(s));
  assert.deepEqual(
    extra,
    [],
    `dead entries, so a rename went unnoticed: ${extra}`,
  );
});

test("every key the map names resolves in the source catalogue", () => {
  for (const [status, key] of Object.entries(HOST_STATUS_KEY)) {
    assert.ok(
      typeof HOSTS[key] === "string" && HOSTS[key].length > 0,
      `runDetail.hosts.${key} (for status "${status}") is missing from en.json`,
    );
  }
});

test("not-reached is neutral, not a warning", () => {
  // The run did not get to the host. That is neither a pass nor a failure, and
  // colouring it as either is a claim about the host the data does not support.
  const neutral = hostStatusBadge("not-reached");
  assert.ok(!neutral.includes("red") && !neutral.includes("amber"), neutral);
  assert.ok(!neutral.includes("green"), neutral);
});

test("an unknown status falls back to its own text rather than to a key", () => {
  assert.equal(hostStatusKey("something-new"), null);
  // And it still gets a badge, so a new status is legible rather than unstyled.
  assert.ok(hostStatusBadge("something-new").length > 0);
});

test("the two event kinds are among the mapped statuses", () => {
  // The failure drill-down renders `kind` through the same map, so these two
  // must be present or a failure row shows a raw slug.
  for (const kind of ["failed", "unreachable"]) {
    assert.ok(hostStatusKey(kind), `event kind "${kind}" has no message key`);
  }
});
