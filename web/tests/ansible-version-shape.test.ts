import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it } from 'node:test'

/**
 * The Ansible version field agrees with itself about the value's shape
 * (#2413 F1).
 *
 * The field has two states and they disagreed. When the available set is
 * known it is a `<select>` of LINES — `2.21` — built from the server's own
 * availability listing. When it is unknown it is free text, and its
 * placeholder suggested `2.21.5`: a PATCH, which is the one shape the picker
 * never offers.
 *
 * That is not a consistency nit, which is why it is gated rather than just
 * fixed. `effective_ee_image` builds the image reference by interpolating the
 * stored value straight into a tag — `{repository}:{ansible_version}` — and
 * the execution environments publish an IMMUTABLE `X.Y.Z` tag beside a MOVING
 * `X.Y` one. Only the moving tag receives the rebuilds that clear a base-image
 * or ansible-core CVE. So an operator who typed what the placeholder suggested
 * pinned a tag that will never be patched, and nothing would have told them.
 *
 * Derived from the server rather than restated: `DEFAULT_ANSIBLE_LINE` is the
 * one producer of what a line looks like, so a deployment that moved to a
 * different component count would fail here rather than silently teaching the
 * old shape.
 */

const ROOT = join(import.meta.dirname, '..', '..')
const MODELS = readFileSync(join(ROOT, 'services', 'terrapod', 'db', 'models.py'), 'utf8')
const CONFIG = readFileSync(join(import.meta.dirname, '..', 'src', 'i18n', 'config.ts'), 'utf8')

/** `2.21` — the shape a line has, read off the server's own default. */
const defaultLine = (() => {
  const m = MODELS.match(/^DEFAULT_ANSIBLE_LINE = "([^"]+)"/m)
  assert.ok(m, 'could not read DEFAULT_ANSIBLE_LINE — has it been renamed?')
  return m![1]
})()

const locales = (() => {
  const block = CONFIG.match(/export const locales = \[([\s\S]*?)\] as const/)
  assert.ok(block, 'could not find the `locales` array')
  return [...block![1].matchAll(/'([^']+)'/g)].map((m) => m[1])
})()

describe('the ansible-version placeholder suggests a line, not a patch', () => {
  it('reads a two-component default from the server', () => {
    // The premise everything below rests on. If a line ever gains a component
    // this fails here, naming the reason, rather than in a locale assertion.
    assert.match(defaultLine, /^\d+\.\d+$/, `DEFAULT_ANSIBLE_LINE is ${defaultLine}`)
  })

  it('offers an example of exactly that shape in every locale that carries one', () => {
    const components = defaultLine.split('.').length
    let checked = 0
    for (const loc of locales) {
      const cat = JSON.parse(readFileSync(join(import.meta.dirname, '..', 'messages', `${loc}.json`), 'utf8'))
      const value: string | undefined = cat.workspaceDetail?.fields?.ansibleVersionPlaceholder
      if (value === undefined) continue // `en-GB` carries only spelling deltas
      checked += 1
      const versions = [...value.matchAll(/\d+(?:\.\d+)+/g)].map((m) => m[0])
      assert.equal(
        versions.length,
        1,
        `${loc}: the placeholder should name exactly one example, found ${versions.length} in ${JSON.stringify(value)}`,
      )
      assert.equal(
        versions[0].split('.').length,
        components,
        `${loc}: the example is ${versions[0]}, which is not the shape the picker offers ` +
          `(${defaultLine}). A patch pins the IMMUTABLE execution-environment tag, which ` +
          `never receives a patched rebuild.`,
      )
    }
    // A floor, so a renamed key cannot make this pass on nothing.
    assert.ok(checked >= 30, `only ${checked} locales carry the placeholder`)
  })
})
