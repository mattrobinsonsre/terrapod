import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/**
 * The run page must react to `run_updated` (#2119).
 *
 * `run_status_change` is the only event that drove `loadRun()` after mount, and
 * three artifact uploads write columns this page renders without changing any
 * status. On the Terraform path two of them land AFTER the `plan-result` POST
 * that caused the transition — so the Overview's Changes and Cost cards
 * rendered against a row whose numbers had not arrived, and nothing refetched.
 *
 * The server half is gated in `services/tests/api/test_run_column_writes_notify.py`,
 * which derives its subject from the router's source. This is the other half,
 * and it needs a gate of its own for an uncomfortable reason: the two sides are
 * coupled only by an event NAME in a string, so the server can publish
 * faithfully for ever while the client quietly stops listening, and every test
 * on both sides still passes. No E2E assertion covers it either — the existing
 * run specs load the page after a run has reached a terminal state, which is
 * precisely the case that always worked.
 */

const PAGE = fileURLToPath(
  new URL('../src/app/workspaces/[id]/runs/[runId]/page.tsx', import.meta.url),
)
const COST_PANEL = fileURLToPath(new URL('../src/components/cost-panel.tsx', import.meta.url))

/** The event name the server publishes. Changing it must break both sides. */
const EVENT = 'run_updated'

test('the run page refetches the run when it is told the run changed', () => {
  const src = readFileSync(PAGE, 'utf8')
  assert.ok(
    src.includes(`'${EVENT}'`),
    `the page never mentions '${EVENT}', so every late artifact upload is invisible until a reload (#2119)`,
  )

  // Reacting to the event is not enough — it has to reach `loadRun`, which is
  // what refreshes the Changes and Cost cards. Assert the branch that tests the
  // event name also calls it, rather than that both appear in the file.
  const handler = src.slice(src.indexOf('useRunEvents('), src.indexOf('// Each phase'))
  assert.ok(handler.length > 0, 'could not locate the SSE handler; this gate has drifted')
  const refetchBranch = handler
    .split('\n')
    .findIndex((l) => l.includes(`'${EVENT}'`) && !l.includes('changed'))
  assert.ok(refetchBranch >= 0, `no branch in the SSE handler keys on '${EVENT}'`)
  const after = handler.split('\n').slice(refetchBranch, refetchBranch + 6).join('\n')
  assert.ok(
    after.includes('loadRun()'),
    `the '${EVENT}' branch does not call loadRun(), so the run itself is never refetched`,
  )
})

test('the cost tab can be refetched after it has mounted', () => {
  const page = readFileSync(PAGE, 'utf8')
  const panel = readFileSync(COST_PANEL, 'utf8')

  // CostPanel fetches its own estimate. Its effect used to key only on the
  // endpoint, which never changes for a page view — so a panel mounted while
  // the run was still planning kept its 404 for the life of the page.
  assert.ok(
    /refreshKey/.test(panel),
    'cost-panel.tsx has no refreshKey, so a mount-time 404 is permanent (#2119)',
  )
  const deps = panel.slice(panel.indexOf('apiFetch(endpoint)'))
  assert.ok(
    /\}, \[endpoint, t, refreshKey\]\)/.test(deps),
    'refreshKey is declared but absent from the fetch effect deps — the prop is inert',
  )
  assert.ok(
    /<CostPanel[^>]*refreshKey=/.test(page),
    'the run page renders CostPanel without passing refreshKey, so nothing ever bumps it',
  )
})
