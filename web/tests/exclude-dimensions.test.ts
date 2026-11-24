/**
 * The exclusion editor offers exactly the dimensions the SERVER accepts (#2548).
 *
 * Derived from the Python rather than listed here, because a hand-kept list is
 * how the form comes to drop a dimension the API accepts — which is the defect
 * `assignment-rule-editor`'s own docstring is about: a dimension missing from
 * the form is one the form silently discards on save, quietly widening or
 * narrowing a rule somebody set through the API or Terraform.
 *
 * Read as TEXT, deliberately. A test cannot import Python, and re-implementing
 * the server's dimension list in TypeScript would be the second copy that the
 * derivation exists to avoid.
 *
 * `node:test`, not vitest: vitest reports every assertion in this directory as
 * passing and then exits with "no test suite found", which reads as a product
 * failure and is the runner.
 */

import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  BULK_EXCLUDE_DIMENSIONS,
  spellDimension,
  MAX_EXCLUDE_ENTRIES,
  RULE_EXCLUDE_DIMENSIONS,
  entryIsEmpty,
} from '../src/lib/exclude-dimensions.ts'

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.join(here, '..', '..')
const read = (p: string) => readFileSync(path.join(repo, p), 'utf8')

const SEARCH = 'services/terrapod/services/workspace_search_service.py'
const SELF_JOIN = 'services/terrapod/services/varset_self_join.py'
const VARIABLES = 'services/terrapod/api/routers/variables.py'

/** The 14 names in `_explicit_dimensions`, which is the server's own list. */
function serverDimensions(): string[] {
  const src = read(SEARCH)
  const start = src.indexOf('def _explicit_dimensions')
  assert.ok(start > 0, `_explicit_dimensions not found in ${SEARCH}`)
  // The tuple literal immediately following, up to its closing paren.
  const open = src.indexOf('for k in (', start)
  assert.ok(open > 0, 'the dimension tuple moved; teach this reader its new shape')
  const close = src.indexOf('\n            )', open)
  const block = src.slice(open, close)
  const names = [...block.matchAll(/"([a-z_]+)"/g)].map((m) => m[1])
  assert.ok(names.length >= 10, `only parsed ${names.length} dimensions — matcher is wrong`)
  return names
}

/** `RULE_DIMENSIONS_REFUSED`'s keys: refused in a rule, recursively into `exclude`. */
function ruleRefused(): string[] {
  const src = read(SELF_JOIN)
  const start = src.indexOf('RULE_DIMENSIONS_REFUSED: dict[str, str] = {')
  assert.ok(start > 0, 'RULE_DIMENSIONS_REFUSED not found')
  const block = src.slice(start, src.indexOf('\n}', start))
  const names = [...block.matchAll(/^ {4}"([a-z_]+)":/gm)].map((m) => m[1])
  assert.ok(names.length >= 2, `only parsed ${names.length} refused keys`)
  return names
}

describe('the exclusion editor tracks the server', () => {
  it('bulk offers EVERY dimension the server accepts', () => {
    // The issue's own examples need `workspace_ids` and `name_glob`, and the
    // positive bulk filter offers neither — so a narrower set here would make
    // the feature unable to express the thing it was asked for.
    assert.deepEqual([...BULK_EXCLUDE_DIMENSIONS].sort(), [...serverDimensions()].sort())
  })

  // #2696: the POSITIVE filter drifted from the exclusion editor -- the server
  // accepted fourteen dimensions, the exclusion rows offered all fourteen, and
  // `buildFilter` could send only six. So an operator could exclude by a
  // dimension they could not select by.
  //
  // Derived from `buildFilter`'s own source rather than from the markup,
  // because the page holds each dimension in a state variable and a scan of the
  // fieldset under-reports. The wire name is what the function emits, so this
  // reads the kebab-case key out of the assignments and compares the set to the
  // server's, which is itself read from the Python.
  it('buildFilter can send every dimension the server accepts', () => {
    const page = read('web/src/app/admin/bulk-update/page.tsx')
    const start = page.indexOf('function buildFilter()')
    assert.ok(start > 0, 'buildFilter not found -- has it been renamed?')
    // to the next top-level `function` at the same indentation
    const end = page.indexOf('\n  function ', start + 10)
    assert.ok(end > start, 'could not bound buildFilter')
    const body = page.slice(start, end)

    // Both forms the function uses: f['name-prefix'] and f.locked. A dimension
    // whose name needs no quoting is written the second way, so a matcher that
    // reads only the bracket form silently misses it -- which is how a gate
    // comes to pass over the attribute it was written for.
    const sent = new Set<string>()
    for (const m of body.matchAll(/\bf\['([a-z-]+)'\]\s*=/g)) sent.add(m[1])
    for (const m of body.matchAll(/\bf\.([a-zA-Z]+)\s*=/g)) sent.add(m[1])

    const wanted = serverDimensions().map((d) => spellDimension(d, 'kebab'))
    const missing = wanted.filter((w) => !sent.has(w))
    assert.deepEqual(
      missing,
      [],
      `buildFilter cannot send ${missing.length} dimension(s) the server accepts: ` +
        `${missing.join(', ')}. Add a line to buildFilter and a control to the ` +
        `filter fieldset, or the form can exclude by a dimension it cannot ` +
        `select by (#2696).`,
    )

    // The floor binds the matcher rather than the page: if the regexes stop
    // matching, `sent` empties and every dimension reads as missing -- but a
    // matcher that silently narrowed to a handful would still pass the check
    // above once the page shrank with it.
    assert.ok(
      sent.size >= wanted.length,
      `only matched ${sent.size} assignments in buildFilter, below the ` +
        `${wanted.length} dimensions the server accepts -- the matcher has ` +
        `stopped reading the function`,
    )
  })

  it('the assignment rule omits exactly what the server refuses, and nothing else', () => {
    // Offering a refused dimension means an operator filling a control whose
    // only possible outcome is a 422 on save. Omitting an ACCEPTED one is the
    // silent-drop defect. So this is an equality, not a subset.
    const refusedByRule = new Set([...ruleRefused(), 'workspace_ids'])
    const expected = serverDimensions().filter((d) => !refusedByRule.has(d))
    assert.deepEqual([...RULE_EXCLUDE_DIMENSIONS].sort(), [...expected].sort())
  })

  it('`workspace_ids` really is refused in a rule exclude entry', () => {
    // The one name above that is not in RULE_DIMENSIONS_REFUSED, so without
    // this the set it belongs to is asserted from my reading rather than from
    // the server.
    const src = read(VARIABLES)
    assert.match(
      src,
      /cannot use 'workspace_ids' in an exclude entry/,
      'the refusal moved; the rule dimension set above is now unfounded',
    )
  })

  it('the entry cap matches the server', () => {
    const m = read(SEARCH).match(/^MAX_EXCLUDE_ENTRIES = (\d+)$/m)
    assert.ok(m, 'MAX_EXCLUDE_ENTRIES not found')
    assert.equal(MAX_EXCLUDE_ENTRIES, Number(m[1]))
  })
})

describe('an entry that sets no selector is recognised before the server has to', () => {
  it('an untouched row is empty', () => {
    assert.equal(entryIsEmpty({}), true)
  })

  it('a blank name prefix or glob is NOT a selector', () => {
    // The server's own asymmetry (`_BLANK_IS_NOT_A_SELECTOR`): these are gated
    // on truthiness, so a blank one produced a filter that passed the
    // at-least-one-selector check and then built a query with no WHERE clause.
    assert.equal(entryIsEmpty({ name_prefix: '   ' }), true)
    assert.equal(entryIsEmpty({ 'name-glob': '' }), true)
  })

  it('a blank owner email IS a selector', () => {
    // `owner_email` is NOT NULL defaulting to "", so `{"owner_email": ""}` is
    // the only way to select unowned workspaces. Treating it as "untouched"
    // would make that unexpressible.
    assert.equal(entryIsEmpty({ owner_email: '' }), false)
  })

  it('an empty collection is not a selector, a populated one is', () => {
    assert.equal(entryIsEmpty({ labels: {} }), true)
    assert.equal(entryIsEmpty({ workspace_ids: [] }), true)
    assert.equal(entryIsEmpty({ workspace_ids: ['   '] }), true)
    assert.equal(entryIsEmpty({ labels: { tier: 'prod' } }), false)
    assert.equal(entryIsEmpty({ 'workspace-ids': ['ws-1'] }), false)
  })

  it('a false boolean is a selector, not an absence', () => {
    // `locked: false` selects unlocked workspaces. Reading it as unset would
    // drop the dimension and widen the exclusion to every workspace.
    assert.equal(entryIsEmpty({ locked: false }), false)
    assert.equal(entryIsEmpty({ has_vcs: false }), false)
  })

  it('both key spellings are understood', () => {
    // The two surfaces disagree on casing and the component serves both.
    assert.equal(entryIsEmpty({ 'name-prefix': 'x' }), false)
    assert.equal(entryIsEmpty({ name_prefix: 'x' }), false)
  })
})

describe('the refusal that blocks Apply agrees with its own number', () => {
  // Four of the six translators flagged this independently and no gate could
  // see it: the trailing instruction sat OUTSIDE the plural block, so at n=1
  // English read "...so it protects nothing. Fix or remove THEM before
  // applying." A plural pronoun for one exclusion.
  //
  // The placeholders are identical either way (`n`, `rows`), so the i18n
  // completeness gate and the fill writer's own parity check both pass on the
  // broken shape. Only reading it catches it, which is why it is pinned here.
  //
  // Scoped to this ONE key deliberately. The general form -- "no locale may
  // carry text after the plural arms where English does not" -- was measured
  // across all 2581 plural messages in the catalogue: 36 hits, of which 17 are
  // legitimate (`graphs.impact.subtitle`, `adminVcs.access.repositoryCount`
  // and `adminAutodiscovery.propagate.apply`, in languages with no grammatical
  // number plus `en-x-yoda`, which reorders clauses by definition). A gate
  // whose failures are half correct translations is one people learn to
  // silence, so it is documented here as a gap rather than committed.

  /** Text after the final plural construct, brace-matched to any depth. */
  function tailAfterPlural(s: string): string | null {
    const at = s.indexOf('plural,')
    if (at < 0) return null
    const open = s.lastIndexOf('{', at)
    let depth = 0
    let i = open
    for (; i < s.length; i++) {
      if (s[i] === '{') depth++
      else if (s[i] === '}' && --depth === 0) break
    }
    return s.slice(i + 1).trim()
  }

  const messages = path.join(repo, 'web', 'messages')
  const locales = readdirSync(messages)
    .filter((f) => f.endsWith('.json'))
    .map((f) => f.replace(/\.json$/, ''))

  it('every locale keeps the instruction inside the arms', () => {
    // A floor, so a renamed key or a broken reader fails here rather than
    // passing on nothing.
    assert.ok(locales.length >= 30, `only found ${locales.length} catalogues`)

    const stale: string[] = []
    for (const loc of locales) {
      const raw = readFileSync(path.join(messages, `${loc}.json`), 'utf8')
      const v = JSON.parse(raw)?.exclude?.blocksApply
      if (typeof v !== 'string') continue
      const tail = tailAfterPlural(v)
      assert.notEqual(tail, null, `${loc}: blocksApply is no longer a plural message`)
      if (tail !== '') stale.push(`${loc}: ${JSON.stringify(tail)}`)
    }
    assert.deepEqual(
      stale,
      [],
      `these locales hoist the instruction out of the plural, so it cannot agree ` +
        `with the number:\n  ${stale.join('\n  ')}`,
    )
  })

  /** One plural arm's body, brace-matched — a regex cannot read ICU. */
  function arm(s: string, name: string): string {
    const at = s.indexOf(`${name} {`)
    assert.ok(at >= 0, `no \`${name}\` arm`)
    const open = s.indexOf('{', at)
    let depth = 0
    let i = open
    for (; i < s.length; i++) {
      if (s[i] === '{') depth++
      else if (s[i] === '}' && --depth === 0) break
    }
    return s.slice(open + 1, i)
  }

  it('the English arms really do differ in their pronoun', () => {
    // Without this the test above pins a structure whose POINT is unasserted:
    // if the two arms ever say the same thing, there is nothing to agree with
    // and the gate is cosmetic.
    //
    // Brace-matched rather than matched with `[^}]*`, which stops at the first
    // closing brace -- `{rows}`'s -- and so can never reach the pronoun. That
    // version passed on nothing and failed on correct English.
    const en = JSON.parse(readFileSync(path.join(messages, 'en.json'), 'utf8'))
    const v: string = en.exclude.blocksApply
    const one = arm(v, 'one')
    const other = arm(v, 'other')
    assert.match(one, /\bit\b/, `the singular arm no longer says "it": ${one}`)
    assert.match(other, /\bthey\b/, `the plural arm no longer says "they": ${other}`)
    assert.notEqual(one, other, 'the arms are identical, so there is nothing to agree with')
    assert.equal(tailAfterPlural(v), '', 'English itself has hoisted the instruction out')
  })
})
