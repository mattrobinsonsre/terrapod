// One producer for the #719 two-tier confirm() policy (#2413 F13).
//
// `lib/use-confirm.ts` is the hook and its docstring says why: "Use across every
// surface with mutating actions so the two tiers stay identical everywhere."
// Eleven files did. Thirteen others did not — the biggest page in the app
// re-implemented both tiers locally and hand-rolled a third form inline, and
// `isTouch && !window.confirm(...)` appeared nine times across the components.
//
// Every copy agreed at the time, which is exactly the state that precedes
// drift: nothing made them agree, so the first surface to be edited would have
// taken its own view of what prompts where. A comment claiming two
// implementations match is a ledger with no gate behind it.
//
// Run with: npm run test:unit

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))

/** The hook itself, which is the one place allowed to call the primitive. */
const HOOK = join('lib', 'use-confirm.ts')

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) return walk(full)
    return /\.(ts|tsx)$/.test(entry) ? [full] : []
  })
}

const files = walk(SRC).map((full) => ({
  rel: full.slice(SRC.length + 1),
  src: readFileSync(full, 'utf8'),
}))

test('the file list is not empty — a floor, so a moved directory cannot pass on nothing', () => {
  assert.ok(files.length > 50, `expected to walk the whole of src, saw ${files.length} files`)
  assert.ok(
    files.some((f) => f.rel === HOOK),
    'the hook itself was not found, so the exemption below would be meaningless',
  )
})

test('only the hook calls window.confirm', () => {
  const offenders = files
    .filter((f) => f.rel !== HOOK && /window\.confirm/.test(f.src))
    .map((f) => f.rel)
  assert.deepEqual(
    offenders,
    [],
    'these call the primitive directly instead of a tier from useConfirm(): ' +
      offenders.join(', '),
  )
})

test('no file defines its own copy of a tier', () => {
  // The workspace page used to declare both of these as local consts, with the
  // AGENTS.md comment copied above them. Catching the DEFINITION rather than
  // the call is what stops the copy coming back under a different call site.
  const offenders = files
    .filter(
      (f) =>
        f.rel !== HOOK &&
        /(const|function)\s+(confirmDelete|confirmTouchMutation|confirmIrreversible)\b/.test(
          f.src,
        ),
    )
    .map((f) => f.rel)
  assert.deepEqual(offenders, [], 'these redefine a tier: ' + offenders.join(', '))
})

test('the hook still offers all three tiers, and both-modes is one function', () => {
  const hook = files.find((f) => f.rel === HOOK)!.src
  for (const tier of ['confirmIrreversible', 'confirmDelete', 'confirmTouchMutation']) {
    assert.match(hook, new RegExp(`\\b${tier}\\b`), `${tier} is gone from the hook`)
  }
  // `confirmDelete` is documented as the same tier as `confirmIrreversible`. If
  // they ever diverge, the axis has changed and the docstring is wrong.
  assert.match(
    hook,
    /confirmIrreversible: both,\s*\n\s*confirmDelete: both,/,
    'confirmDelete and confirmIrreversible must be the same function',
  )
})
