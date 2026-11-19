// A message key must RESOLVE, not merely exist (#2589).
//
// next-intl resolves `t('state.findings')` under namespace `supplyChain` by
// SPLITTING on `.` and traversing: `messages.supplyChain.state.findings`. So a
// catalogue that stores the flat key `"state.findings"` inside the
// `supplyChain` object has the key and cannot render it — every call throws
// and the surface shows its fallback.
//
// **The completeness gate structurally cannot see this.** It flattens both
// shapes with `.` joins, so flat `"state.findings"` and nested
// `{"state":{"findings":…}}` produce the SAME path and compare equal. It
// reports a namespace that cannot render as complete, in every locale at once.
//
// That is exactly what happened: 29 keys were committed flat, and the build,
// tsc, ESLint, the completeness gate and 791 unit tests all passed while the
// page could not render a single string on it.

import { strict as assert } from 'node:assert'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const MESSAGES = join(dirname(fileURLToPath(import.meta.url)), '..', 'messages')

/** Every leaf path in a catalogue, by the traversal next-intl performs. */
function leafPaths(node: unknown, prefix: string[] = [], out: string[] = []): string[] {
  if (typeof node === 'string') {
    out.push(prefix.join('.'))
    return out
  }
  if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
      leafPaths(v, [...prefix, k], out)
    }
  }
  return out
}

const CATALOGUES = readdirSync(MESSAGES).filter((f) => f.endsWith('.json'))

test('the catalogue set is non-empty', () => {
  // A floor: a glob that stops matching would make every assertion below pass
  // on nothing, which is the failure this whole file is about.
  assert.ok(CATALOGUES.length >= 30, `only found ${CATALOGUES.length} catalogues`)
})

for (const file of CATALOGUES) {
  test(`${file}: no key contains a dot, because next-intl would traverse it`, () => {
    const doc = JSON.parse(readFileSync(join(MESSAGES, file), 'utf8')) as Record<string, unknown>

    const offenders: string[] = []
    const walk = (node: unknown, prefix: string[]): void => {
      if (!node || typeof node !== 'object') return
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
        // A dot in a key is never addressable: `t('a.b')` looks for a nested
        // `b` under `a`, so a literal `"a.b"` key is unreachable whatever its
        // value is.
        if (k.includes('.')) offenders.push([...prefix, k].join(' > '))
        walk(v, [...prefix, k])
      }
    }
    walk(doc, [])

    assert.deepEqual(
      offenders,
      [],
      `${file} stores ${offenders.length} key(s) containing a dot. next-intl splits on '.' ` +
        `and traverses, so these can never resolve — the completeness gate cannot see it ` +
        `because it flattens both shapes to the same path. Nest them instead: ` +
        offenders.slice(0, 3).join('; '),
    )
  })
}

test('every locale exposes the same resolvable paths as the source', () => {
  // Complementary to the completeness gate rather than a duplicate of it: that
  // one compares flattened key sets, which cannot distinguish the two shapes.
  // This compares paths derived by TRAVERSAL, so a locale that nested a group
  // the source left flat (or the reverse) fails here.
  const en = JSON.parse(readFileSync(join(MESSAGES, 'en.json'), 'utf8'))
  const source = new Set(leafPaths(en))
  assert.ok(source.size > 3000, `en.json resolved only ${source.size} paths`)

  for (const file of CATALOGUES) {
    if (file === 'en.json') continue
    // en-GB is a spelling-delta OVERRIDE and is deliberately a subset.
    const paths = new Set(leafPaths(JSON.parse(readFileSync(join(MESSAGES, file), 'utf8'))))
    const extra = [...paths].filter((p) => !source.has(p))
    assert.deepEqual(
      extra,
      [],
      `${file} resolves ${extra.length} path(s) the source does not: ${extra.slice(0, 3).join(', ')}`,
    )
  }
})
