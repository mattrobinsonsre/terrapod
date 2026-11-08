import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it } from 'node:test'

/**
 * Two navigation controls on one screen do not share a label (#2413 F7).
 *
 * The workspace page has two levels of navigation visible at once: the tab
 * strip across the top, and the inventory tab's own sub-view row. Both carried
 * an entry reading "Variables", in two idioms, going to different places — so
 * the only thing distinguishing them was which one you happened to click.
 *
 * The idioms staying different is CORRECT and is not what was wrong: an
 * underlined tab strip and a pill row are how two levels of navigation are
 * meant to read differently. The label was the collision, and the inventory
 * one is now "Inventory variables" — composed in each locale from that
 * catalogue's own word for the inventory and its own word for variables, in
 * the grammar its own `nav.label` already demonstrates.
 *
 * Asserted PER LOCALE rather than for `en` alone, which is the whole point: a
 * collision is a property of the rendered words, so it can exist in one
 * language and not another. Two labels that differ in English can collapse to
 * the same string in a language that compounds them, and no key-parity gate
 * can see that.
 */

const CONFIG = readFileSync(join(import.meta.dirname, '..', 'src', 'i18n', 'config.ts'), 'utf8')
const locales = (() => {
  const block = CONFIG.match(/export const locales = \[([\s\S]*?)\] as const/)
  assert.ok(block, 'could not find the `locales` array')
  return [...block![1].matchAll(/'([^']+)'/g)].map((m) => m[1])
})()

/** Not navigation: the accessible name of the control itself. */
const NOT_A_DESTINATION = new Set(['label', 'selectLabel'])

describe('the workspace tab strip and the inventory sub-nav share no label', () => {
  it('holds in every locale that carries both', () => {
    let checked = 0
    /** Locales that cannot tell their own tabs apart, which this cannot hold
     *  to telling them apart from a sub-nav. Collected rather than skipped, and
     *  asserted below against the one locale where it is the joke working. */
    const selfColliding: string[] = []
    for (const loc of locales) {
      const cat = JSON.parse(
        readFileSync(join(import.meta.dirname, '..', 'messages', `${loc}.json`), 'utf8'),
      )
      const wd = cat.workspaceDetail
      const tabs: Record<string, string> | undefined = wd?.tabs
      const nav: Record<string, string> | undefined = wd?.inventory?.nav
      // `en-GB` carries only spelling deltas, so it has neither block.
      if (!tabs || !nav) continue
      const destinations = Object.entries(tabs).filter(([k]) => !NOT_A_DESTINATION.has(k))
      // A locale that cannot tell its OWN tabs apart cannot be held to telling
      // them apart from a sub-nav. DERIVED rather than an allow-list of locale
      // names: `en-x-marklar` replaces every noun with one word, so its tab
      // strip already reads "Marklar", "Marklars" and "Run marklars" for
      // several destinations. That is the joke working, and it is the only
      // locale in the tree where this holds — measured, not assumed. A second
      // lossy locale exempts itself; a real locale that starts colliding with
      // itself has a worse problem than this gate.
      const values = destinations.map(([, v]) => v.trim().toLowerCase())
      if (new Set(values).size !== values.length) {
        // RECORDED, not silently skipped (#2425 §2.6). The exemption itself is
        // sound -- `en-x-marklar` replaces every noun with one word, so its tab
        // strip legitimately reads "Marklar", "Marklars" and "Run marklars" --
        // but `continue` meant a REAL locale that started colliding with itself
        // dropped out of the gate with nothing saying so, and the floor below
        // carried enough slack to absorb it. The set is asserted after the loop.
        selfColliding.push(loc)
        continue
      }
      checked += 1
      const tabLabels = new Map<string, string>()
      for (const [k, v] of destinations) tabLabels.set(v.trim().toLowerCase(), k)
      for (const [k, v] of Object.entries(nav)) {
        if (NOT_A_DESTINATION.has(k)) continue
        const clash = tabLabels.get(v.trim().toLowerCase())
        assert.equal(
          clash,
          undefined,
          `${loc}: inventory sub-view "${k}" and workspace tab "${clash}" both read ` +
            `${JSON.stringify(v)}, so the only thing telling them apart is which one you clicked`,
        )
      }
    }
    // Exactly the lossy locale, and nothing else. A silent `continue` is how a
    // real locale's collision becomes invisible; an assertion makes adding a
    // second lossy locale a deliberate edit with a reason, which is what the
    // old comment promised ("a second lossy locale exempts itself") and what
    // nothing enforced.
    assert.deepEqual(
      selfColliding.sort(),
      ['en-x-marklar'],
      'these locales cannot tell their own workspace tabs apart, so this gate skipped ' +
        'them: ' +
        selfColliding.join(', ') +
        '. `en-x-marklar` is expected -- it replaces every noun with one word. Any ' +
        'other locale here has a worse problem than this gate and needs fixing, not ' +
        'adding to this list.',
    )
    // A floor, so a renamed block cannot make this pass on nothing.
    assert.ok(checked >= 29, `only ${checked} locales carry both blocks and distinguish their own tabs`)
  })
})
