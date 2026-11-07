// The autodiscovery rule form's two populate paths cover the same fields, and
// its templated settings come from the shared field table (#2413 F4).
//
// WHY THE FIRST ONE EXISTS. `openEditForm` seeds every field of the form;
// `resetForm` seeded forty-six of sixty-one. So clicking New Rule after editing
// a rule produced a "brand-new" rule carrying the edited one's engine, its
// parallelism, its execution-environment sizing, all three configure-start
// booleans, BOTH dynamic-inventory grants, host-key checking, its collections,
// its AI policy and context, and its fork-PR grant. Two of those grants let
// third-party code run on a runner and one decides whether a fork's pull
// request gets a speculative plan, so the inheritance was a silent widening,
// not a cosmetic leftover.
//
// Fifteen missing setters is the kind of omission nobody finds by reading, and
// a hand-kept list of "the fields a reset must clear" would be the same
// omission in a second place. So BOTH SIDES ARE DERIVED FROM THE PAGE: the
// subject is what `openEditForm` seeds -- which is, by construction, exactly
// the form's field set -- and the requirement is that `resetForm` calls each
// one. There is no ledger to go stale, and a field added to the form arrives in
// the subject the moment its edit-path seed does.
//
// `setShowForm` is the single exclusion and it is a real asymmetry rather than
// an excuse: `openEditForm` opens the form as its last act, and `resetForm` is
// called BY the two paths that open and close it, so clearing it there would
// shut the form the operator just asked for.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (...parts: string[]) => readFileSync(path.join(here, '..', ...parts), 'utf8')

const page = read('src', 'app', 'admin', 'autodiscovery', 'page.tsx')
const fields = read('src', 'lib', 'workspace-fields.ts')

/** The body of a top-level `function name() { … }` in the page. */
function body(name: string): string {
  const re = new RegExp(`\\n  function ${name}\\([^)]*\\) \\{\\n([\\s\\S]*?)\\n  \\}\\n`)
  const m = page.match(re)
  assert.ok(m, `${name} not found in admin/autodiscovery/page.tsx`)
  return m[1]
}

/** The `setX` names a block calls. */
const settersCalled = (src: string): Set<string> =>
  new Set([...src.matchAll(/\b(set[A-Z]\w*)\(/g)].map((m) => m[1]))

test('resetForm clears every field openEditForm seeds', () => {
  const seeded = settersCalled(body('openEditForm'))
  const cleared = settersCalled(body('resetForm'))

  // A floor, so a renamed function or a changed brace shape fails here rather
  // than passing on an empty subject — the way this whole class of gate stops
  // meaning anything.
  assert.ok(
    seeded.size >= 55,
    `only ${seeded.size} setters found in openEditForm; the matcher has stopped matching`,
  )

  const missing = [...seeded].filter((s) => s !== 'setShowForm' && !cleared.has(s)).sort()
  assert.deepEqual(
    missing,
    [],
    `resetForm leaves ${missing.length} field(s) holding the previously edited rule's ` +
      `value, so "New Rule" inherits them: ${missing.join(', ')}`,
  )
})

test('resetForm does not clear more than the form holds', () => {
  // The other direction, which keeps the first test honest: if `resetForm`
  // grew a setter `openEditForm` does not seed, then one of the two is wrong
  // about what the form contains and the subject above is no longer derived
  // from the form at all.
  const seeded = settersCalled(body('openEditForm'))
  const cleared = settersCalled(body('resetForm'))
  const extra = [...cleared].filter((s) => !seeded.has(s)).sort()
  assert.deepEqual(
    extra,
    [],
    `resetForm clears field(s) openEditForm never seeds: ${extra.join(', ')}. ` +
      `Either the edit path is missing a seed, or these are not form fields.`,
  )
})

// ── the shared sections (#2413 F4) ────────────────────────────────────────

test('the rule form renders the shared groups rather than its own sections', () => {
  // The structural half of F4/F5: the twelve groups in the table's order, each
  // with a real heading, instead of four hand-written blocks whose titles were
  // `<label>`s pointing at no control.
  assert.match(page, /import \{ Field, Group, type GroupCtx \} from '@\/components\/workspace-field-groups'/)
  assert.match(page, /GROUPS\.map\(\(g\) => \(/)
  assert.match(page, /title=\{tWs\(`groups\.\$\{g\}`\)\}/)
})

/**
 * An ELEMENT, not a mention.
 *
 * A bare `/<details\\b/` is satisfied by the comment that explains why the
 * `<details>` went -- which is the matcher-reports-it-as-present failure in its
 * least useful direction, and it fired on the first run of the test below. A
 * JSX tag in this tree opens a line, where a mention sits inside a sentence.
 */
const opensTag = (tag: string) => new RegExp(`^\\s*<${tag}[\\s>/]`, 'm')

test('the element matcher can match, so a negative means something', () => {
  // The matcher's own self-test. Without it, narrowing `opensTag` into
  // something that matches nothing turns both assertions below green, and the
  // page could collapse again with the gate reporting success.
  assert.ok(opensTag('section').test(page), 'opensTag matches nothing; the negatives are vacuous')
  assert.ok(opensTag('form').test(page), 'opensTag matches nothing; the negatives are vacuous')
  assert.ok(!opensTag('marquee').test(page), 'opensTag matches anything; the negatives are vacuous')
})

test('nothing on the rule form collapses', () => {
  // The decision F4 records: sections carry navigability and collapse does not,
  // and it costs an interaction before an operator can see what a form offers.
  assert.ok(!opensTag('details').test(page), 'the rule form still collapses a section')
  assert.ok(!opensTag('summary').test(page), 'the rule form still has a <summary>')
})

test('the rule form reads its group structure from the table, not its own copy', () => {
  // `GROUPS` is the shared order and `LABEL_KEY` the shared labels, so the rule
  // template and the setting it produces are described in the same words. A
  // page that reimplemented either would be the drift the table exists to stop.
  assert.match(page, /\bGROUPS,\n/)
  assert.match(page, /\bLABEL_KEY,\n/)
  assert.match(page, /return tWs\(LABEL_KEY\[attr\]\)/)
  // And the table's side, so the page cannot name a group the table lacks.
  assert.match(fields, /export const GROUPS = \[/)
})

test('the five fields the rule derives or scopes itself are not offered as template settings', () => {
  // Each is a judgement rather than an omission, and a control for any of them
  // would take a value the rule overwrites or say one thing twice:
  //
  //   name, working-directory  -- derived from `name-template` and from the
  //                              directory a match was found in
  //   vcs-repo-url, vcs-branch -- the rule's own scan target, above
  //   trigger-prefixes         -- the rule's path patterns already scope it,
  //                              and it is exempt in the parity gate
  //
  // `vcs-connection-id` is deliberately NOT in this list: it has no control
  // either, but it IS in `ruleValues`, because three VCS fields gate on it and
  // leaving it out would disable them on every rule for ever.
  for (const attr of [
    'name',
    'working-directory',
    'vcs-repo-url',
    'vcs-branch',
    'trigger-prefixes',
    'security-scan-skip-rules',
  ]) {
    assert.ok(
      !new RegExp(`attr="${attr}"`).test(page),
      `the rule form offers ${attr} as a template setting, which it must not`,
    )
  }
  assert.match(page, /'vcs-connection-id': vcsConnectionId/)
})
