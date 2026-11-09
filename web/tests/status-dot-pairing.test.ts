// A status dot never carries state on its own (#2446).
//
// `StatusDot` gives the dot an `aria-label`, which settles the screen-reader
// case and settles nothing for a sighted user: an accessible name is not a
// visual presentation, and WCAG 1.4.11 asks for 3:1 on a graphic required to
// understand the content.
//
// That 3:1 is NOT REACHABLE with one class, which is why this is a structural
// gate rather than a colour one. Measured against each theme's own surfaces:
//
//            light (row #dbeafe .. page #f8fafc)   dark (#020618 .. #1d293d)
//   500 row  online 2.64  degraded 2.62  FAIL      4.54 .. 5.06  PASS
//   600 row  online 4.05  degraded 4.12  PASS      2.96 .. 2.28  FAIL
//
// The light ladder sits at luminance .75-.95 and the dark at .01-.05, so a
// single mid-chroma fill is squeezed from both ends. Per-theme remapping of
// `*-500` would drag every other consumer of those stops along with it.
//
// So the word beside the dot is what states the status, and the dot reinforces
// it. The pools table's phone reflow is the site that proves this gate earns
// its keep: below `md` the STATUS column is hidden, and that reflow carried the
// dot inline WITHOUT the text — eight pixels of hue at 2.64:1 as the only
// signal a sighted phone user got.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'

/** Every file in web/src that renders a StatusDot, from the tree. */
function sites(): { file: string; src: string }[] {
  const files = execFileSync('grep', ['-rl', '<StatusDot', 'src', '--include=*.tsx'], {
    encoding: 'utf8',
  })
    .split('\n')
    .filter(Boolean)
  return files.map((file) => ({ file, src: readFileSync(file, 'utf8') }))
}

/** Each `<StatusDot ... />` with the label expression it was handed. */
function calls(src: string): { call: string; label: string; at: number }[] {
  const out: { call: string; label: string; at: number }[] = []
  for (const m of src.matchAll(/<StatusDot\b[\s\S]*?\/>/g)) {
    const label = m[0].match(/label=\{([\s\S]*?)\}\s*(?:\/>|\n)/)
    assert.ok(label, 'a StatusDot with no label= :\n' + m[0])
    out.push({ call: m[0], label: label[1].trim(), at: m.index! })
  }
  return out
}

test('every StatusDot renders its label as visible text beside the dot', () => {
  const all = sites()
  assert.ok(all.length > 0, 'no file renders a StatusDot — has the component been renamed?')

  let checked = 0
  const bad: string[] = []
  for (const { file, src } of all) {
    for (const { label, at, call } of calls(src)) {
      checked++
      // The enclosing JSX, bounded generously either side of the call. A site
      // that renders the text puts the SAME expression in a second place here.
      const window = src.slice(Math.max(0, at - 400), at + call.length + 400)
      const occurrences = window.split(label).length - 1
      if (occurrences < 2) {
        bad.push(
          `${file}: the dot is the only thing carrying ${JSON.stringify(label)}. ` +
            'Render it as text beside the dot — see the note on FILL in status-dot.tsx ' +
            'for why the colour cannot do this on its own.',
        )
      }
    }
  }
  // A floor, so a matcher that stops matching fails instead of passing on nothing.
  assert.ok(checked >= 3, `only inspected ${checked} StatusDot call sites, expected at least 3`)
  assert.deepEqual(bad, [])
})

test('the phone reflow of the pools table carries the status word', () => {
  // Named explicitly because it is the site that regressed, and because it is
  // the only one where the desktop STATUS column is hidden — so a future
  // "tidy-up" that drops the text here looks harmless and is not.
  const src = readFileSync('src/app/admin/agent-pools/page.tsx', 'utf8')
  const reflow = src.match(/data-testid="pool-row-status-mobile"[\s\S]*?<\/span>\s*\)\}/)
  assert.ok(reflow, 'no pool-row-status-mobile reflow — has the testid moved?')
  assert.match(reflow[0], /<StatusDot/, 'the reflow should still show the dot')
  assert.match(
    reflow[0],
    /<span className="text-xs text-slate-400 capitalize">\s*\{t\(/,
    'the reflow must render the status word, not only the dot',
  )
})

test('the FILL table stays exhaustive over EntityStatus', () => {
  // The grey catch-all this replaced made offline, cert-expired and "unknown"
  // indistinguishable. A new status must look wrong, not look fine.
  const src = readFileSync('src/components/status-dot.tsx', 'utf8')
  const union = src.match(/export type EntityStatus =([^\n]+)/)
  assert.ok(union, 'no EntityStatus union')
  const states = [...union[1].matchAll(/'([^']+)'/g)].map((m) => m[1]).sort()
  const table = src.match(/const FILL: Record<EntityStatus, string> = \{([\s\S]*?)\n\}/)
  assert.ok(table, 'no FILL table')
  const keys = [...table[1].matchAll(/^\s*'?([a-z-]+)'?:\s*'bg-/gm)].map((m) => m[1]).sort()
  assert.deepEqual(keys, states, 'FILL must cover exactly the statuses the union declares')
  assert.ok(states.length >= 4, `expected at least 4 statuses, found ${states.length}`)
})
