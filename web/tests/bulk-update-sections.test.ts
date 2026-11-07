// The fleet form renders the SHARED sections and keeps its OWN controls
// (#2413 F4).
//
// Four forms set workspace attributes and each used to invent its own
// arrangement. `workspace-fields.ts` draws the line: which group an attribute
// belongs to and which boolean gates it are properties of the ATTRIBUTE and
// the same on every surface, while the control type and what an empty value
// means are per surface. This file pins the half that is easy to get wrong in
// a later "simplification" — and every assertion here is DERIVED from `FIELDS`
// rather than from a list, so a new attribute is in scope the moment it is
// declared.
//
// Source-introspection, because none of it is visible to a behavioural test:
// the page is a client component the bare `node --test` runner cannot render,
// and the two defects below produce a form that looks right.
//
// Run with: npm run test:unit

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { FIELDS, GROUPS } from '../src/lib/workspace-fields.ts'

const here = path.dirname(fileURLToPath(import.meta.url))
const bulk = readFileSync(
  path.join(here, '..', 'src', 'app', 'admin', 'bulk-update', 'page.tsx'),
  'utf8',
)

/** The gate booleans the fleet form offers as a tri-state select. */
const offeredGates = new Set(
  [...bulk.matchAll(/^ {4}'([a-z0-9-]+)': u[A-Z]/gm)].map((m) => m[1]),
)

/** `gateOff('attr')` call sites. */
const gated = new Set([...bulk.matchAll(/gateOff\('([a-z0-9-]+)'\)/g)].map((m) => m[1]))

test('the sections come from GROUPS, not from a hand-written order', () => {
  // Walking `GROUPS` is what stops this form drifting from the other three.
  // A literal list of sections would pass a reader's eye and go stale the
  // first time a group is added or reordered.
  assert.match(bulk, /GROUPS\.filter\(\(g\) => groupBody\[g\]\)\.map\(/)
  assert.match(bulk, /title=\{tWs\(`groups\.\$\{g\}`\)\}/)
  // The shared wrapper, not a local one: the heading level and type scale are
  // the thing being made consistent.
  assert.match(bulk, /from '@\/components\/workspace-field-groups'/)
  // A floor. `groupBody` keying every group it offers is what the filter above
  // reads, so a renamed key would make the render produce nothing at all.
  const keyed = [...bulk.matchAll(/^ {4}([a-z][A-Za-z]*): \(/gm)].map((m) => m[1])
  const known = keyed.filter((k) => (GROUPS as readonly string[]).includes(k))
  assert.ok(
    known.length >= 10,
    `only ${known.length} of the 12 shared groups are keyed in groupBody — the ` +
      'matcher has probably stopped matching the map rather than the map having emptied',
  )
  // `identity` holds `name` and `engine`, which are NEVER_BULK_SETTABLE, so
  // this surface contributes nothing to it and the filter must drop it rather
  // than print a heading over an empty grid.
  assert.ok(!known.includes('identity'), 'identity has no fleet-settable field')
})

test('a dependent is disabled only when its gate is EXPLICITLY off', () => {
  // THE decision this file exists for. The shared `dependencyMet` reads an
  // absent gate as off, which is right where a gate holds a concrete boolean
  // and wrong here: this form's selects are tri-state and '' means "leave this
  // alone", so `dependencyMet` would disable all thirteen Configure dependents
  // whenever Configure itself is untouched — forbidding the commonest fleet
  // operation there is ("I am not changing configure-enabled, but I do want to
  // set the playbook across the fleet").
  assert.match(bulk, /booleanValues\[dep\] === 'false'/)
  for (const shared of ['dependencyMet', 'fieldGateMet']) {
    assert.ok(
      !new RegExp(`\\b${shared}\\(`).test(bulk),
      `the fleet form must not call ${shared} — it reads '' as off, which ` +
        'disables every dependent of an untouched gate',
    )
  }
})

test('every dependent whose gate this form offers is gated', () => {
  // Derived from `FIELDS`, so a new `dependsOn` is covered the day it is
  // declared. The converse is deliberately not asserted: a field whose gate
  // this form does not offer (`vcs-connection-id`) can never be explicitly
  // false, so asking is harmless and uniform.
  const reachable = FIELDS.filter(
    (f) => f.dependsOn !== undefined && offeredGates.has(f.dependsOn),
  ).map((f) => f.attr)
  assert.ok(
    reachable.length >= 15,
    `only ${reachable.length} gate-reachable dependents found — the gate map ` +
      'matcher has probably stopped matching rather than the gates having gone',
  )
  const ungated = reachable.filter((a) => !gated.has(a))
  assert.deepEqual(
    ungated,
    [],
    `these dependents render enabled against a gate the operator can turn off: ${ungated}`,
  )

  // The above is a PRESENCE check per attribute, and a presence check is not
  // an application check: a field with two controls could lose the gate on one
  // of them and still appear here. The `uSet*` fields are exactly that shape —
  // an opt-in checkbox plus the control it reveals — and which attribute each
  // one writes is derivable from `buildUpdate`, so they can be held to both.
  const optIn = [...bulk.matchAll(/if \(uSet[A-Za-z]+\)\s*\n?\s*u\['([a-z0-9-]+)'\]/g)].map(
    (m) => m[1],
  )
  assert.ok(optIn.length >= 6, `only ${optIn.length} uSet* payload writes parsed`)
  const sites = (attr: string) =>
    [...bulk.matchAll(new RegExp(`gateOff\\('${attr}'\\)`, 'g'))].length
  const halfGated = optIn.filter((a) => reachable.includes(a) && sites(a) < 2)
  assert.deepEqual(
    halfGated,
    [],
    'these opt-in fields gate one of their two controls and leave the other ' +
      `live — the checkbox and the control it reveals both need it: ${halfGated}`,
  )
})

test('the gate DISABLES and never hides, and the disabled state is visible', () => {
  // #2413 F20: seeing the option is how an operator learns it exists. The
  // native dark-theme disabled rendering is nearly indistinguishable from an
  // ordinary input, so without the `disabled:` variants "disabled but visible"
  // reads as "enabled and ignoring me".
  assert.match(bulk, /disabled:opacity-50 disabled:cursor-not-allowed/)
  // The gate must reach `disabled`, never a conditional render.
  assert.ok(
    !/\{!?gateOff\([^)]*\) &&/.test(bulk),
    'a gate must disable a control, not decide whether to render it',
  )
})

test('the fleet form keeps its own controls rather than adopting Field', () => {
  // Measured incompatible on three counts `Field` has no room for: a tri-state
  // "unchanged" value with no concrete value to show, a `uSet*` opt-in
  // checkbox whose sentence is the only label six fields have, and a payload
  // that must omit a key entirely rather than send a cleared one. `Group` is
  // shared; `Field` is not.
  assert.ok(
    !/\bField\b[,}]/.test(bulk.slice(0, bulk.indexOf('export default'))),
    'the fleet form imports Field, whose Control writes on every keystroke ' +
      'with no unchanged state — clearing a number box would send 0 to the fleet',
  )
  // The ids the operations gate test locates, unchanged by the move.
  assert.match(bulk, /id="bu-plan-apply-enabled"/)
  assert.match(bulk, /id="bu-configure-enabled"/)
})

test('the two collections with no FIELDS entry sit outside the group loop', () => {
  // Run tasks and notification configurations are collections attached to a
  // workspace and upserted by name, not workspace attributes, so the shared
  // table does not carry them and the registry-driven loop cannot render them.
  // They keep the section idiom so the form still reads as one thing.
  const attrs = new Set(FIELDS.map((f) => f.attr))
  for (const key of ['run-tasks', 'notification-configurations']) {
    assert.ok(!attrs.has(key), `${key} is in FIELDS now — move it into a group`)
    assert.ok(bulk.includes(`u['${key}']`), `${key} is no longer written to the payload`)
  }
  assert.match(bulk, /\{tWs\('tabs\.runTasks'\)\}/)
  assert.match(bulk, /\{tWs\('tabs\.notifications'\)\}/)
})
