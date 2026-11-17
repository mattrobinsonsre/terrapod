import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'

/**
 * The `<select>` picker is styled by us, not by the operating system (#2599).
 *
 * A native select's open list is drawn as a separate browser- or OS-level
 * window, outside the page, so nothing in our stylesheet reaches it. That is
 * the documented limitation, and on its own it is only a cosmetic mismatch.
 * The reason these assertions exist is the second, sharper problem: because the
 * picker is its own window it carries its own scale factor, and Chromium has
 * shipped bugs in that path (crbug/41329817 draws it at twice size and in the
 * wrong position). An operator met exactly that. `appearance: base-select`
 * removes the second window, which is the only fix available to a page.
 *
 * These are text assertions over `globals.css` on purpose. The property is
 * whether the RULE is present and correctly shaped; a DOM test would need a
 * browser that supports `base-select`, so it would silently pass as a no-op on
 * one that does not -- which is the engine where the rule matters least and the
 * assertion would mislead most.
 */

const CSS = readFileSync(join(import.meta.dirname, '..', 'src', 'app', 'globals.css'), 'utf8')

/** The `@supports (appearance: base-select) { ... }` block, brace-matched. */
function supportsBlock(): string {
  const at = CSS.indexOf('@supports (appearance: base-select)')
  assert.notEqual(at, -1, 'the @supports (appearance: base-select) block is gone')
  const open = CSS.indexOf('{', at)
  let depth = 0
  for (let i = open; i < CSS.length; i++) {
    if (CSS[i] === '{') depth++
    else if (CSS[i] === '}') {
      depth--
      if (depth === 0) return CSS.slice(open + 1, i)
    }
  }
  assert.fail('the @supports block is unbalanced')
}

test('the picker is rendered as page content, for the select AND the picker', () => {
  const block = supportsBlock()
  // Both halves are required. `appearance: base-select` on the select alone
  // leaves the picker drawn by the platform, which is the whole defect.
  assert.match(block, /select\s*,\s*::picker\(select\)\s*\{[^}]*appearance:\s*base-select/)
})

test('every base-select rule is INSIDE @supports, so Firefox keeps a working control', () => {
  const block = supportsBlock()
  // DECLARATIONS, not mentions: the rule's own comment explains `base-select`
  // several times, and the `@supports` condition names it once. All three are
  // prose as far as the cascade is concerned. A declaration ends in `;`.
  const decl = /appearance:\s*base-select\s*;/g
  const inBlock = (block.match(decl) ?? []).length
  const inFile = (CSS.match(decl) ?? []).length
  assert.ok(inBlock > 0, 'no appearance: base-select declaration inside @supports')
  assert.equal(
    inFile,
    inBlock,
    'an appearance: base-select declaration sits outside @supports — on an ' +
      'engine without support that is unconditional, not progressive enhancement',
  )
})

test('options keep the app-wide 44px tap-target floor', () => {
  const block = supportsBlock()
  // The options are real content now, so the touch rules apply to them exactly
  // as they do to any other control (#719).
  assert.match(block, /select option\s*\{[^}]*min-height:\s*2\.75rem/)
})

test('the picker bounds its own height, so a long list cannot run off-screen', () => {
  const block = supportsBlock()
  // A platform-drawn popup scrolled itself. Page content does not, unless told.
  assert.match(block, /::picker\(select\)\s*\{[^}]*max-height:/)
  assert.match(block, /::picker\(select\)\s*\{[^}]*overflow-y:\s*auto/)
})

test('selected state is carried by :checked, not only by :hover', () => {
  const block = supportsBlock()
  // Hover is a pointer affordance; touch has none. The state must not depend
  // on it, or the selected row is invisible on a phone.
  assert.match(block, /select option:checked\s*\{[^}]*background:/)
})

test('the picker is styled from theme tokens, so it follows light and dark', () => {
  const block = supportsBlock()
  // EVERY `::picker(select)` block, because there are two: the combined
  // `select, ::picker(select)` appearance rule and the standalone styling one.
  // A first-match regex finds the combined one, which carries no colour at all.
  const blocks = [...block.matchAll(/::picker\(select\)\s*\{([^}]*)\}/g)].map((m) => m[1])
  assert.ok(blocks.length > 0, 'no ::picker(select) rule')
  const styled = blocks.find((b) => /background:/.test(b))
  assert.ok(styled, 'no ::picker(select) rule sets a background')
  // A literal colour would be correct in one theme and wrong in the other.
  assert.match(styled, /var\(--color-slate-800\)/)
  assert.doesNotMatch(styled, /#[0-9a-fA-F]{3,8}\b/, 'a literal hex colour cannot theme')
})
