// No surface may offer a rule dimension the server refuses (#2621).
//
// `varset_self_join.RULE_DIMENSIONS_REFUSED` refuses three dimensions on a
// variable-set assignment rule -- in a POSITIVE term as firmly as inside an
// `exclude` entry -- with a 422 naming the dimension. Until 2.0 the provider's
// `assignment_rule` block and the web editor each offered two of them as
// positive controls, so an operator filled in a selector whose only possible
// outcome was a refusal, and a practitioner got an APPLY-time 422 against a
// schema description asserting it worked.
//
// The exclusion rows never had the defect, because they derive their list from
// `RULE_REFUSED` in `exclude-dimensions.ts`. The positive surfaces each wrote
// their controls by hand, which is why only they drifted -- and why this gate
// reads the SERVER's set rather than that constant: a gate keyed on the web
// constant would pass if both drifted together.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it } from 'node:test'

const repo = path.join(import.meta.dirname, '..', '..')
const read = (p: string) => readFileSync(path.join(repo, p), 'utf8')

/** The server's own refused set, read out of the Python. */
function serverRefused(): string[] {
  const src = read('services/terrapod/services/varset_self_join.py')
  const start = src.indexOf('RULE_DIMENSIONS_REFUSED')
  assert.ok(
    start > 0,
    'RULE_DIMENSIONS_REFUSED not found — this gate reads it by shape, so update the matcher',
  )
  const open = src.indexOf('{', start)
  let depth = 0
  let close = open
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++
    else if (src[i] === '}') {
      depth--
      if (depth === 0) {
        close = i
        break
      }
    }
  }
  assert.ok(close > open, 'could not brace-match the refused-dimension dict')
  // Keys at the dict's own indentation only: it maps a dimension to a REASON,
  // and the reasons name endpoints and permissions in quotes of their own.
  const names = [...src.slice(open, close).matchAll(/\n {4}"([a-z_]+)":/g)].map((x) => x[1])
  assert.ok(names.length >= 3, `only parsed ${names.length} refused dimensions`)
  return names
}

// Each surface, and how IT spells a dimension. Keyed per surface rather than
// searched with one pattern, because a single matcher would read a mention in a
// comment as an offered control -- and every one of these files now carries a
// comment naming all three.
const SURFACES: { file: string; offers: (src: string, dim: string) => boolean; what: string }[] = [
  {
    file: 'provider/internal/resources/variable_set/resource.go',
    what: 'the terrapod_variable_set assignment_rule block',
    // a schema entry, not a mention: `"drift_status": schema.XAttribute{`
    offers: (src, dim) => new RegExp(`"${dim}":\\s*schema\\.\\w+\\{`).test(src),
  },
  {
    file: 'web/src/components/assignment-rule-editor.tsx',
    what: 'the assignment-rule editor',
    // a control that WRITES the dimension, which a comment cannot do
    offers: (src, dim) => new RegExp(`setField\\(\\s*'${dim}'`).test(src),
  },
]

describe('no surface offers a rule dimension the server refuses', () => {
  it('parses the server set', () => {
    const refused = serverRefused()
    assert.ok(refused.includes('drift_status') && refused.includes('locked'), refused.join(','))
  })

  for (const s of SURFACES) {
    it(`${s.what} offers none of them`, () => {
      const src = read(s.file)
      const bad = serverRefused().filter((d) => s.offers(src, d))
      assert.deepEqual(
        bad,
        [],
        `${s.file} offers ${bad.join(', ')} on a variable-set assignment rule, which the ` +
          `server refuses with a 422 naming the dimension — so the control's only possible ` +
          `outcome is a refusal. Remove it, or change RULE_DIMENSIONS_REFUSED.`,
      )
    })

    // The floor: a matcher that stopped matching would report every surface
    // clean. Each one must still recognise a dimension the server ACCEPTS.
    it(`${s.what}'s matcher still recognises an accepted dimension`, () => {
      const src = read(s.file)
      const accepted = ['vcs_connection_id', 'owner_email', 'has_vcs'].filter((d) => s.offers(src, d))
      assert.ok(
        accepted.length > 0,
        `the matcher for ${s.file} found no accepted dimension either, so it has stopped ` +
          `reading the file and its clean result above means nothing`,
      )
    })
  }
})
