/**
 * A workspace status badge links to the view that EXPLAINS the status (#2413 F16).
 *
 * The case the review named: *"Like when you click **errored** on a workspace you
 * get taken to the run which errored."* F16's delivery listed seven surfaces and
 * shipped six; this is the seventh, and the one the review actually asked for.
 *
 * Two halves, because either alone is satisfiable while the finding persists:
 *
 *  - the RULE — `runHref` appends `?view=` when the phase is derivable and falls
 *    back to the bare run link when it is not. A wrong view is worse than the
 *    page's default, because it asserts the phase it opened is the one that
 *    explains the outcome;
 *  - the WIRING — the component's `<Link>` goes through `runHref` rather than
 *    building the path itself. Reverting that is a one-line edit that no test of
 *    the rule can see.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runHref } from '../src/lib/run-phases.ts'

const here = path.dirname(fileURLToPath(import.meta.url))
const SRC = readFileSync(path.join(here, '..', 'src', 'components', 'workspace-status-badges.tsx'), 'utf8')

describe('a status badge deep-links to the explaining phase', () => {
  const base = '/workspaces/ws-1/runs/run-2'

  it('an errored run lands on the phase that was last to START', () => {
    // The whole reason `status` cannot do this job: `errored` does not say which
    // phase errored, and the phase that failed recorded no finish — so the last
    // START is the only honest signal.
    assert.equal(
      runHref('ws-1', 'run-2', { 'planning-at': 'a', 'applying-at': 'b' }),
      `${base}?view=apply`,
    )
    assert.equal(
      runHref('ws-1', 'run-2', { 'planning-at': 'a', 'applying-at': 'b', 'configuring-at': 'c' }),
      `${base}?view=configure`,
    )
    assert.equal(runHref('ws-1', 'run-2', { 'planning-at': 'a' }), `${base}?view=plan`)
  })

  it('falls back to the bare run link rather than guessing', () => {
    // Three real cases, not hypotheticals: a workspace-level condition has no
    // run phase; `drifted` deliberately sends null because its run is the drift
    // run rather than the latest; and a server predating `phase-started-at`
    // sends nothing at all.
    assert.equal(runHref('ws-1', 'run-2', {}), base)
    assert.equal(runHref('ws-1', 'run-2', null), base)
    assert.equal(runHref('ws-1', 'run-2', undefined), base)
  })

  it('the component routes its Link through runHref, not a path of its own', () => {
    // The wiring half. Without this, reverting the `<Link href>` to a template
    // literal leaves every assertion above green and the finding unfixed —
    // which is exactly how surface 7 came to be missing while F16 read as
    // shipped.
    assert.match(SRC, /href=\{runHref\(workspaceId, runId, phaseStarts\)\}/)
    assert.doesNotMatch(
      SRC,
      /href=\{`\/workspaces\/\$\{workspaceId\}\/runs\/\$\{runId\}`\}/,
      'the Link builds its own path again — route it through runHref',
    )
  })

  it('and the caller threads the phase starts, so the prop is not always absent', () => {
    // `phaseStarts` is optional and absent falls back, so a caller that stops
    // passing it degrades silently to the old behaviour. Assert the four call
    // sites on the workspaces page still pass it.
    const page = readFileSync(
      path.join(here, '..', 'src', 'app', 'workspaces', 'page.tsx'),
      'utf8',
    )
    const threaded = page.match(/phaseStarts=\{phaseStarts\}/g) ?? []
    assert.equal(threaded.length, 4, `only ${threaded.length} of 4 call sites thread phaseStarts`)
    assert.match(page, /const \{ def, runId, phaseStarts \} = resolveStatus\(ws\)/)
  })
})
