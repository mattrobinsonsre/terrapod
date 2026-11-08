import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it } from 'node:test'

/**
 * The run kind is one vocabulary, in BOTH renders (#2413 F11).
 *
 * The finding was that `Plan + apply` — the DEFAULT kind — was plain text in a
 * cell where the four markers beside it are badges, so the two kinds of run
 * were typeset as two kinds of thing. The fix made it a badge.
 *
 * This exists because the fix landed in ONE of the two renders. The workspace
 * Runs tab keeps a desktop `<table>` and a phone `MobileCardList`, both in the
 * DOM at every width, and the first attempt badged the table and left the card
 * reading `text-slate-400`. On a phone the default kind was still prose beside
 * a `Configure` badge — the exact defect, surviving in the half that was not
 * looked at.
 *
 * So the gate is about the RELATIONSHIP between the two renders rather than
 * about either one: every place the run kind is rendered must use the same
 * badge shape. A per-render assertion cannot see a drift between them, which
 * is what happened.
 */

const PAGE = readFileSync(
  join(import.meta.dirname, '..', 'src', 'app', 'workspaces', '[id]', 'page.tsx'),
  'utf8',
)

/** The badge shape the run-kind markers share. */
const BADGE = 'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium'

/**
 * The opening tag of the element a label sits in.
 *
 * No fixed look-back window, which the first version of this had and which was
 * the defect: one `<span>` carries a multi-line `title=`, so 400 characters did
 * not reach its opening tag, `lastIndexOf` returned -1 and the gate reported a
 * badge that was there as missing. A matcher that fails toward a FINDING is the
 * expensive direction, so it scans the whole prefix and refuses rather than
 * guessing when it cannot find a tag.
 *
 * **It looks for ANY element, not for `<span` (#2425 §2.6.)** It used to search
 * `lastIndexOf('<span', at)`, which does not fail when the label's own element
 * is something else -- it walks PAST it to the preceding sibling, and on this
 * page the preceding sibling is usually a badge. So un-badging a label by
 * changing its `<span>` to a `<div>` kept the gate green while reading the
 * neighbour's classes, which is the one mistake it exists to catch.
 *
 * The nearest preceding `<` that starts an element is the enclosing tag: JSX
 * cannot put a `<` inside an attribute value unescaped, and a closing `</` is
 * not an opening tag, so the scan is exact rather than heuristic.
 */
function openingTag(at: number): string {
  const prefix = PAGE.slice(0, at)
  const tag = prefix.search(/<[A-Za-z][^<]*$/)
  assert.ok(
    tag >= 0,
    `no enclosing element found before offset ${at}; the label is not inside a tag, ` +
      'which means this page no longer has the shape this gate reads',
  )
  return PAGE.slice(tag, at)
}

describe('both renders typeset the run kind the same way', () => {
  it('renders the default kind in every render, and as a badge each time', () => {
    // Anchored on the string the label comes from rather than on a class, so a
    // restyle moves with it and a MISSING render is what fails.
    const sites = [...PAGE.matchAll(/phaseWord\('typePlanApply'\)/g)].map((m) => m.index!)
    assert.equal(
      sites.length,
      2,
      `expected the desktop table and the phone card, found ${sites.length} renders of the ` +
        'default run kind — if a third surface now shows it, badge it there too',
    )
    for (const at of sites) {
      assert.ok(
        openingTag(at).includes(BADGE),
        'one render of the default run kind is not a badge — it was prose on the phone ' +
          'card while the desktop table was a badge, which is the defect F11 exists to fix',
      )
    }
  })

  it('uses that same badge shape for the kinds it sits beside', () => {
    // The comparison that makes "same kind of thing" mean something: a badge
    // nobody else shares is just a differently-shaped outlier.
    for (const key of ['typeDestroy', 'typeConfigure', 'typeSavedPlan']) {
      const sites = [...PAGE.matchAll(new RegExp(`t\\('runs\\.${key}'\\)`, 'g'))].map(
        (m) => m.index!,
      )
      assert.ok(sites.length >= 2, `${key} should render in both the table and the card`)
      for (const at of sites) {
        assert.ok(openingTag(at).includes(BADGE), `${key} is not a badge in one of its renders`)
      }
    }
  })
})
