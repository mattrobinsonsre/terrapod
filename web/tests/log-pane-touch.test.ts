// The run log pane never scrolls inside itself on touch (#2217, #719).
//
// On a coarse pointer the house rule is that the PAGE is the scroll container:
// no inner scrollbar nested inside a scrolling page. The pane honours that by
// gating every one of its sizing classes on `fine:`, so on touch it carries no
// cap, no floor and no overflow — it grows to its content and the page scrolls.
// A floor on a phone would only add dead space under a short log.
//
// #2217 replaced the `70vh` cap with a measured pixel value, which is exactly
// the kind of change that invites someone to "simplify" the prefix away while
// moving the value. Nothing else would catch it: the desktop tests pass either
// way, and on touch the symptom is a nested scroller that only shows up on a
// real phone.
//
// This is a source-introspection gate because the property is about the classes
// the component emits, which no unit test of the sizing function can see.
//
// Two things in here were written the obvious way first and were both blind,
// which is why they are the shape they are:
//
//   * the utility matcher TOKENISES on quotes as well as whitespace. Anchored
//     on `(^|\s)` it silently inspected ZERO utilities on the reserve wrapper,
//     whose class sits in a ternary so its `min-h-` is preceded by `'`. The
//     gate reported a pass with the floor — the half that governs streaming —
//     entirely unexamined.
//   * the subject is counted PER GROUP against an exact expected number, not as
//     one loose total. Three unrelated `font-mono whitespace-pre-wrap` error
//     blocks elsewhere in the file also matched the body marker, so a combined
//     floor of four was satisfied without the log pane being in the set at all.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const PAGE = fileURLToPath(
  new URL('../src/app/workspaces/[id]/runs/[runId]/page.tsx', import.meta.url),
)

/** Utilities that would create a nested scroller or pin a height on touch. */
const SIZING = /^((?:[a-z-]+:)*)((?:max-h|min-h|overflow-y)-.+)$/

/** Split a className expression into candidate utility tokens. Quotes and
 *  braces are separators as much as spaces are: in `reserve ? 'fine:min-h-[…]'
 *  : undefined` the utility is flush against a quote. */
const tokens = (text: string): string[] => text.split(/[\s'"`{}()]+/).filter(Boolean)

type Group = { kind: 'pane' | 'reserve'; text: string }

/** The log pane's own class strings, found by the component's own markers
 *  rather than by line number, so an edit that moves them cannot make this gate
 *  read nothing:
 *    - pane  — the scrolling <pre> bodies (font-mono, pre-wrap, and an
 *              overflow-y, which is what distinguishes them from the plain
 *              font-mono error blocks elsewhere in the file);
 *    - reserve — the wrapper that holds the height open while a phase streams. */
function logPaneClassStrings(src: string): Group[] {
  const out: Group[] = []
  for (const m of src.matchAll(/className=(?:"([^"]*)"|\{([^}]*)\})/g)) {
    const text = m[1] ?? m[2] ?? ''
    const near = src.slice(Math.max(0, m.index - 200), m.index)
    if (text.includes('font-mono') && text.includes('whitespace-pre-wrap') && /overflow-y-/.test(text)) {
      out.push({ kind: 'pane', text })
    } else if (/min-h-|max-h-/.test(text) && near.includes('log-reserve')) {
      out.push({ kind: 'reserve', text })
    }
  }
  return out
}

/** Exact counts, not a floor: a renamed marker drops a group to zero and fails
 *  here, instead of being covered by whatever else happens to match. */
function subject(src: string): Group[] {
  const groups = logPaneClassStrings(src)
  const panes = groups.filter((g) => g.kind === 'pane')
  const reserve = groups.filter((g) => g.kind === 'reserve')
  assert.equal(
    panes.length,
    3,
    `expected 3 scrolling log panes (indexed, colour, plain) — found ${panes.length}. Either a pane lost its inner scroller or this gate's markers have moved and it is checking the wrong thing.`,
  )
  assert.equal(
    reserve.length,
    1,
    `expected exactly 1 log-reserve wrapper carrying a height — found ${reserve.length}. The streaming floor (#1547) is the half a whitespace-anchored matcher silently skipped once already.`,
  )
  return groups
}

test('every sizing class on the log pane is gated on a fine pointer', () => {
  const src = readFileSync(PAGE, 'utf8')
  let checked = 0

  for (const { kind, text } of subject(src)) {
    for (const token of tokens(text)) {
      const m = SIZING.exec(token)
      if (!m) continue
      const [, variants, utility] = m
      checked += 1
      assert.ok(
        variants.split(':').filter(Boolean).includes('fine'),
        `\`${utility}\` on the log pane (${kind}) is not gated on \`fine:\` — on touch that nests a scroller inside the scrolling page, or pins a height a phone cannot give, which is what #719 forbids. Found in: ${text.trim()}`,
      )
    }
  }

  // 7 = three panes × (max-h + overflow-y) + the reserve's min-h. Exact,
  // because this is the number the whitespace-anchored version got wrong: it
  // reported 6 and called it a pass.
  assert.equal(
    checked,
    7,
    `inspected ${checked} sizing utilities, expected 7 (3 panes × max-h + overflow-y, plus the reserve floor). A count that drifts low means tokens are being skipped, which is how this gate passed with the floor unexamined.`,
  )
})

test('the pane never carries a viewport-relative height', () => {
  // `70vh` is what #2217 removed, and it failed in BOTH directions: as a cap it
  // left a gap that moved with the window, and as a floor it could not be
  // satisfied at all once the content above exceeded 30vh, so it guaranteed the
  // overflow it was meant to prevent. The replacement is measured in pixels
  // from the pane's own position, so a `vh` here is a regression by definition.
  const src = readFileSync(PAGE, 'utf8')
  for (const { kind, text } of subject(src)) {
    for (const token of tokens(text)) {
      const m = SIZING.exec(token)
      if (!m) continue
      assert.ok(
        !/\d+(vh|dvh|svh|lvh)\b/.test(m[2]),
        `\`${m[2]}\` on the log pane (${kind}) sizes against the viewport rather than against the space actually left below it (#2217). Found in: ${text.trim()}`,
      )
    }
  }
})
