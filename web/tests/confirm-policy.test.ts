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

/** The source with comments and string literals blanked, line numbers intact.
 *
 *  A mention in prose is not a call, and prose is unavoidable here: every file
 *  that confirms anything explains the two-tier policy in a comment, and the
 *  policy's name IS the primitive's. Measured, a matcher without this reports
 *  three false positives -- a `{/* ... *\/}` JSX block comment whose
 *  continuation lines carry no leading asterisk, and two `hint-tiers.ts` ledger
 *  entries whose VALUES read "a confirm() body". Skipping lines that merely
 *  START with a comment marker catches none of the three.
 *
 *  Replaced with spaces rather than removed, so a reported line number is the
 *  line in the file -- a gate that points at the wrong line is one nobody
 *  trusts. A real call survives: `confirm(t('x'))` becomes `confirm(t(' '))`.
 */
function blankProse(src: string): string {
  const keepNewlines = (m: string) => m.replace(/[^\n]/g, ' ')
  return src
    .replace(/\/\*[\s\S]*?\*\//g, keepNewlines)
    .replace(/\/\/[^\n]*/g, keepNewlines)
    .replace(/'(?:[^'\\\n]|\\.)*'/g, keepNewlines)
    .replace(/"(?:[^"\\\n]|\\.)*"/g, keepNewlines)
    .replace(/`(?:[^`\\]|\\.)*`/g, keepNewlines)
}

test('the file list is not empty — a floor, so a moved directory cannot pass on nothing', () => {
  assert.ok(files.length > 50, `expected to walk the whole of src, saw ${files.length} files`)
  assert.ok(
    files.some((f) => f.rel === HOOK),
    'the hook itself was not found, so the exemption below would be meaningless',
  )
})

test('only the hook calls the confirm primitive, qualified OR bare', () => {
  // It used to match `window.confirm` alone (#2425 §2.3), so a bare global
  // `confirm(...)` passed -- and four did: two token actions and two policy-set
  // deletes. Each happened to prompt in both modes, which is the right tier for
  // an irreversible action, so nothing looked wrong; what was wrong is that the
  // tier was decided at the call site instead of by the one producer, which is
  // the state this whole file exists to prevent.
  //
  // `\bconfirm\s*\(` does not match `confirmDelete(`, `confirmIrreversible(`,
  // `confirmTouchMutation(` or `useConfirm(`: the regex needs the open paren
  // immediately after `confirm`, and the word boundary is case-sensitive.
  const PRIMITIVE = /\bwindow\.confirm\s*\(|(?<![.\w])confirm\s*\(/
  const offenders = files
    .filter((f) => f.rel !== HOOK)
    .flatMap((f) =>
      blankProse(f.src)
        .split('\n')
        .map((line, i) => ({ line, n: i + 1 }))
        .filter(({ line }) => PRIMITIVE.test(line))
        .map(({ n }) => `${f.rel}:${n}`),
    )
  assert.deepEqual(
    offenders,
    [],
    'these call the primitive directly instead of a tier from useConfirm(): ' +
      offenders.join(', '),
  )
})

test('prose is blanked and line numbers survive it', () => {
  const src = ["const a = 1", "// confirm(x)", "/* confirm(y)", "   confirm(z) */", "const s = 'confirm(w)'", "if (!confirm(real)) return"].join('\n')
  const out = blankProse(src)
  assert.equal(out.split('\n').length, 6, 'line count changed, so reported numbers would be wrong')
  const PRIMITIVE = /\bwindow\.confirm\s*\(|(?<![.\w])confirm\s*\(/
  const hits = out.split('\n').map((l, i) => (PRIMITIVE.test(l) ? i + 1 : 0)).filter(Boolean)
  assert.deepEqual(hits, [6], 'only the real call on line 6 should survive')
})

test('the primitive matcher discriminates — fixtures, not a count', () => {
  // The recorded lesson from §2.4: a matcher that silently narrows shrinks what
  // is checked and nothing says so. A count over the tree cannot tell a removed
  // call from a broken pattern, so each form is pinned by a literal.
  const PRIMITIVE = /\bwindow\.confirm\s*\(|(?<![.\w])confirm\s*\(/
  for (const bad of [
    'if (!confirm(t("x"))) return',
    'if (!window.confirm(t("x"))) return',
    'const ok = confirm ("spaced")',
    '  if (!confirm(msg)) return',
  ]) {
    assert.ok(PRIMITIVE.test(bad), `should have matched: ${bad}`)
  }
  for (const good of [
    'if (!confirmDelete(t("x"))) return',
    'if (!confirmIrreversible(t("x"))) return',
    'if (!confirmTouchMutation(t("x"))) return',
    'const { confirmDelete } = useConfirm()',
    'await page.confirm(x)',
    'const reconfirm = confirmed',
  ]) {
    assert.ok(!PRIMITIVE.test(good), `should NOT have matched: ${good}`)
  }
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

// ── what this file deliberately does NOT assert ──────────────────────────────
//
// "every handler issuing a destructive mutation carries a tier call in the same
// function body" is the property #2425 §2.3 really wants, and it is not
// committable as written. The derivable subject is the message keys -- every
// `*Confirm` key should sit inside a tier call -- and measured against the tree
// that is 13 false positives out of 28: `runs.needsConfirm` is a badge label,
// `runs.destroyAllConfirm` is a span, `queuePlanConfirm` is a phase word, the
// two `state.*Confirm` keys render inside a bespoke dialog component rather than
// a native prompt, and two more are `hint-tiers.ts` ledger entries.
//
// A gate whose findings are mostly correct code teaches people to silence it
// (the #1915 precedent), so the gap is documented here instead of enforced. What
// IS enforced -- one producer, no local redefinition, no direct primitive, and
// all three tiers present -- means a handler that does confirm cannot confirm
// INCONSISTENTLY. Whether it confirms at all is reviewed, and the phone-width
// suite drives the touch tier end to end.
