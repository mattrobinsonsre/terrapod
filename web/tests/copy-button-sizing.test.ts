// The shared copy control must not impose a height on a dense toolbar (#2448).
//
// `CopyButton` shipped with `px-3 py-2 min-h-[2.25rem]` baked into its base
// class — a 36px floor, chosen for the tap target on the write-once secrets the
// component exists for. Its siblings in the run-log toolbar are `px-2 py-1`,
// 24px. So the one place the control is CONDITIONAL on content — it renders
// only once the log has output — grew its row by 12px the moment output
// arrived, and the log pane below it is measured from that row: a reserved-
// height pane (#1547, #2217) silently lost 12px against an end-to-end
// tolerance of 2.
//
// That failure is only visible by measuring a real browser, which no unit test
// does, so this file pins the two properties that produce it:
//
//   * the `compact` variant imposes NO height, and its padding is DERIVED from
//     the padding its toolbar siblings actually use rather than written down
//     here — a hardcoded string would drift the moment the toolbar's idiom
//     changed, which is the drift that caused this;
//   * the run-log call site asks for `compact`. The default is deliberately
//     the roomy variant, because the mobile tap-target rule is a hard
//     requirement and a standalone secret is the common case; that choice is
//     what makes this assertion necessary rather than redundant.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const COMPONENT = 'src/components/copy-button.tsx'
const RUN_PAGE = 'src/app/workspaces/[id]/runs/[runId]/page.tsx'

const component = readFileSync(COMPONENT, 'utf8')
const runPage = readFileSync(RUN_PAGE, 'utf8')

/** The variant table, read out of the component's own source. */
function sizing(): Record<string, string> {
  const block = component.match(/const SIZING: Record<Size, string> = \{([\s\S]*?)\n\}/)
  assert.ok(block, `no SIZING table in ${COMPONENT} — has it been renamed?`)
  const out: Record<string, string> = {}
  for (const m of block[1].matchAll(/^\s*(\w+):\s*'([^']*)',/gm)) out[m[1]] = m[2]
  return out
}

/** Every padding class the run-log toolbar's own <button>s carry. */
function siblingPaddings(): Set<string> {
  const out = new Set<string>()
  for (const m of runPage.matchAll(/className="((?:px-\d|py-\d)[^"]*text-xs[^"]*)"/g)) {
    const pad = m[1]
      .split(/\s+/)
      .filter((c) => /^p[xy]?-/.test(c))
      .sort()
      .join(' ')
    if (pad) out.add(pad)
  }
  return out
}

test('the variant table has both variants and nothing silently dropped', () => {
  const s = sizing()
  assert.deepEqual(Object.keys(s).sort(), ['compact', 'default'])
})

test('compact imposes no height', () => {
  const { compact } = sizing()
  // A height floor is the defect. `min-h-`, `h-` and a `py` past the row's own
  // are each a way to write one.
  assert.ok(!/\bmin-h-/.test(compact), `compact must not set a height floor: ${compact}`)
  assert.ok(!/\bh-\[/.test(compact), `compact must not set a height: ${compact}`)
})

test('default does impose one, so the two variants are genuinely different', () => {
  // Without this the table could collapse to one behaviour and every other
  // assertion here would still pass.
  const { compact, default: roomy } = sizing()
  assert.ok(/\bmin-h-/.test(roomy), `the default variant should carry a tap target: ${roomy}`)
  assert.notEqual(compact, roomy)
})

test('compact matches the padding its toolbar siblings actually use', () => {
  const pads = siblingPaddings()
  assert.ok(pads.size > 0, `found no sibling buttons in ${RUN_PAGE} — has the toolbar moved?`)
  const compact = sizing()
    .compact.split(/\s+/)
    .filter((c) => /^p[xy]?-/.test(c))
    .sort()
    .join(' ')
  assert.ok(
    pads.has(compact),
    `compact padding ${JSON.stringify(compact)} is not one its siblings use: ${[...pads]
      .map((p) => JSON.stringify(p))
      .join(', ')}`,
  )
})

test('the run-log copy control asks for compact', () => {
  const call = runPage.match(/<CopyButton\b[\s\S]*?testId="run-log-copy"[\s\S]*?\/>/)
  assert.ok(call, 'no run-log CopyButton — has testId="run-log-copy" moved?')
  assert.match(call[0], /size="compact"/, 'the run-log control must be compact')
})

test('the run-log control keeps the accessible name and tooltip it replaced', () => {
  // The inline button it replaced carried `log.copyAria` and `log.copyTitle`.
  // The component falls back to the bare "Copy", which is ambiguous on a page
  // holding several copy controls, and would have dropped the tooltip saying
  // the copy is the PLAIN text — the distinction that toolbar's Color/Plain
  // mode exists to make.
  const call = runPage.match(/<CopyButton\b[\s\S]*?testId="run-log-copy"[\s\S]*?\/>/)!
  assert.match(call[0], /ariaLabel=\{t\('log\.copyAria'\)\}/)
  assert.match(call[0], /title=\{t\('log\.copyTitle'\)\}/)
})

test('the component honours both, and the failure title still wins', () => {
  assert.match(component, /ariaLabel \?\? label \?\? t\('copy\.button'\)/)
  assert.match(component, /title=\{outcome === 'failed' \? t\('copy\.failedTitle'\) : title\}/)
})
