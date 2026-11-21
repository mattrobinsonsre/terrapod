/**
 * Where the propagation options sit in the request body (#2621 item 50).
 *
 * `propagate` and `dry_run` are read off the TOP LEVEL of the PATCH body by
 * `_bool_option(body, ...)`, as siblings of `data` — not out of
 * `data.attributes`. Both default to `false`, so an option that drifts into
 * the attributes object does not error: the endpoint saves the rule and
 * propagates NOTHING, the response carries no `meta.propagation`, and the
 * dialog renders neither a table nor its empty state. A silent no-op in the
 * one place an operator is being shown a blast radius.
 *
 * Source-introspection because the defect is a position, not a behaviour: a
 * fixture that only asserts "a PATCH went out" passes either way.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const page = readFileSync(
  path.join(here, '..', 'src', 'app', 'admin', 'autodiscovery', 'page.tsx'),
  'utf8',
)

/** One function's body, brace-matched from its declaration. */
function body(name: string): string {
  const decl = `function ${name}(`
  const at = page.indexOf(decl)
  assert.notEqual(at, -1, `${name} not found — has it been renamed?`)
  const open = page.indexOf('{', page.indexOf(')', at))
  let depth = 0
  for (let i = open; i < page.length; i++) {
    if (page[i] === '{') depth++
    else if (page[i] === '}') {
      depth--
      if (depth === 0) return page.slice(open, i + 1)
    }
  }
  throw new Error(`unbalanced braces in ${name}`)
}

describe('the propagation options are sent beside `data`, never as attributes', () => {
  // The floor: if the extractor stops finding real bodies, every assertion
  // below becomes vacuously true against an empty string.
  it('extracts bodies of a plausible size', () => {
    assert.ok(body('buildRuleAttrs').length > 2000, 'buildRuleAttrs body too small')
    assert.ok(body('runPropagationDryRun').length > 400, 'dry-run body too small')
    assert.ok(body('applyPropagation').length > 400, 'apply body too small')
  })

  it('keeps both options out of the rule attributes entirely', () => {
    const attrs = body('buildRuleAttrs')
    assert.ok(!/\bpropagate\b/.test(attrs), '`propagate` must not be a rule attribute')
    assert.ok(!/\bdry_run\b/.test(attrs), '`dry_run` must not be a rule attribute')
  })

  it('sends propagate AND dry_run on the dry run', () => {
    const fn = body('runPropagationDryRun')
    assert.match(fn, /\n\s+propagate: true,/)
    assert.match(fn, /\n\s+dry_run: true,/)
    // Both as siblings of `data`, i.e. after the data object has closed.
    const dataAt = fn.indexOf('data: {')
    assert.ok(dataAt !== -1 && fn.indexOf('propagate: true') > dataAt)
  })

  it('sends propagate and NOT dry_run on the apply', () => {
    const fn = body('applyPropagation')
    assert.match(fn, /\n\s+propagate: true,/)
    // `dry_run: true` on the apply would roll the whole thing back, so the
    // operator would confirm a fleet write that silently never happened.
    assert.ok(!/\bdry_run\b/.test(fn), '`dry_run` must not be sent on the apply')
  })

  it('builds the payload from the one shared producer', () => {
    // A second inline attrs object would let the dry run describe a payload
    // the apply does not send -- a table about something else.
    for (const fn of ['runPropagationDryRun', 'applyPropagation']) {
      assert.match(body(fn), /attributes: buildRuleAttrs\(\)/, `${fn} must use buildRuleAttrs`)
    }
    assert.equal(
      (page.match(/const attrs: Record<string, unknown> = \{/g) ?? []).length,
      1,
      'exactly one attrs producer',
    )
  })
})
