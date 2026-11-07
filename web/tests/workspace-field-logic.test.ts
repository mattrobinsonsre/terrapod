// The two pieces of real logic behind the shared workspace forms (#2253).
//
// `dependencyMet` decides whether a field is MEANINGFUL. It used to decide
// whether the field was RENDERED at all — #2253 replaced a disable-and-explain
// idiom with hiding, on the grounds that a disabled interval input still shows
// an 86400 it is not using.
//
// #2413 F20 reverses that: nothing on these forms is hidden by a gate, because
// seeing an option is how an operator learns it exists. The 86400 objection is
// real and did not go away — it is answered by the field's hint saying the
// value is not in effect, not by removing the control. So `dependencyMet` now
// feeds `disabled` rather than a render decision, and the render decision is
// `fieldApplies`, which is ENGINE applicability and nothing else.
//
// `groupSummary` used to be the second piece of logic here, deciding what a
// COLLAPSED group admitted to holding so it could not conceal a non-default
// value. It is gone with the collapsing (#2413 F4): nothing collapses, so
// nothing stands in place of a group’s own fields and there is nothing to
// summarise.
//
// It could not be unit-tested while it lived in a file holding JSX, since
// the runner is bare `node --test` with no transpiler. That is why the table
// moved to `src/lib`.
//
// Run with: npm run test:unit

import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  FIELD_BY_ATTR,
  FIELDS,
  dependencyMet,
  fieldApplies,
  fieldGateMet,
  groupFields,
  toAttributes,
} from '../src/lib/workspace-fields.ts'

const label = (attr: string) => attr

test('a field with no gate is always meaningful', () => {
  const spec = FIELD_BY_ATTR['name']
  assert.ok(spec, 'name should be a declared field')
  assert.equal(dependencyMet(spec, {}), true)
})

test('a gated field follows its gate', () => {
  const interval = FIELD_BY_ATTR['drift-detection-interval-seconds']
  assert.equal(interval.dependsOn, 'drift-detection-enabled')
  assert.equal(dependencyMet(interval, { 'drift-detection-enabled': true }), true)
  assert.equal(dependencyMet(interval, { 'drift-detection-enabled': false }), false)
})

test('an ABSENT gate reads as off, which is the fail-closed direction', () => {
  // A dependent shown against a gate nobody set is the worse error: it offers
  // an operator a control that does nothing. Absent must mean hidden.
  const interval = FIELD_BY_ATTR['drift-detection-interval-seconds']
  assert.equal(dependencyMet(interval, {}), false)
  assert.equal(dependencyMet(interval, { 'drift-detection-enabled': undefined }), false)
  assert.equal(dependencyMet(interval, { 'drift-detection-enabled': '' }), false)
  // The string 'false' arrives from a select, and must not read as truthy.
  assert.equal(dependencyMet(interval, { 'drift-detection-enabled': 'false' }), false)
})

// The six `groupSummary` tests that stood here went with the collapsing
// (#2413 F4). They asserted what a SHUT group announced -- a count for a
// list in both its array and map shapes, set-or-unset for an optional
// scalar, and that no defaults table was ever consulted -- which was real
// and is now unreachable: nothing collapses, so nothing is announced in
// place of its own fields. Deleted rather than skipped, because a test for
// a mechanism that does not exist is the same debt as a flag nothing reads.

test('groupFields filters by engine', () => {
  const tfExec = groupFields('execution', 'terraform').map((f) => f.attr)
  const pulumiExec = groupFields('execution', 'pulumi').map((f) => f.attr)
  assert.ok(tfExec.includes('terragrunt-enabled'), 'terragrunt is a Terraform concept')
  assert.ok(!pulumiExec.includes('terragrunt-enabled'), 'and must not reach a Pulumi workspace')
  assert.ok(pulumiExec.includes('pulumi-bind-plan'), 'bind-plan is a Pulumi concept')
  assert.ok(!tfExec.includes('pulumi-bind-plan'), 'and must not reach a Terraform workspace')
})

test('toAttributes NEVER emits an alias beside its canonical key', () => {
  // The API derives `auto-apply` from `auto-apply-mode` and refuses a request
  // carrying both, so emitting the alias is a 422 rather than a redundancy.
  const { attributes } = toAttributes(
    { 'auto-apply-mode': 'create', 'auto-apply': true, 'agent-pool-id': 'apool-1' },
    { create: true },
  )
  assert.equal(attributes['auto-apply-mode'], 'create')
  assert.ok(!('auto-apply' in attributes), 'the superseded boolean must not be sent')
  assert.ok(!('agent-pool-id' in attributes), 'the pre-multi-pool singular must not be sent')
})

test('toAttributes returns vcs-connection separately, not as an attribute', () => {
  // It is a JSON:API relationship. Placed in `attributes` the API ignores it,
  // so the workspace is created with no VCS binding and nothing reports why.
  const { attributes, vcsConnectionId } = toAttributes(
    { 'vcs-connection-id': 'vcsconn-7', 'vcs-repo-url': 'https://example.test/r' },
    { create: true },
  )
  assert.equal(vcsConnectionId, 'vcsconn-7')
  assert.ok(!('vcs-connection-id' in attributes))
  assert.equal(attributes['vcs-repo-url'], 'https://example.test/r')
})

test('engine is emitted on create and withheld on a save', () => {
  // Backwards either way is a real defect: omitted at create, every workspace
  // is Terraform and Pulumi is uncreatable from the UI; sent on a save, it is
  // an attribute no PATCH path accepts.
  const created = toAttributes({ engine: 'pulumi' }, { create: true }).attributes
  assert.equal(created.engine, 'pulumi')
  const saved = toAttributes({ engine: 'pulumi' }, {}).attributes
  assert.ok(!('engine' in saved), 'engine must not be sent on a save')
})

test('an undefined value is omitted rather than sent as null', () => {
  const { attributes } = toAttributes({ name: 'ws', 'default-limit': undefined }, { create: true })
  assert.equal(attributes.name, 'ws')
  assert.ok(!('default-limit' in attributes))
})

test('every declared field belongs to a declared group', () => {
  // A typo'd group id would render the field nowhere, silently — `Group` only
  // asks for the fields of the groups it knows about.
  const ids = new Set(FIELDS.map((spec) => spec.group))
  for (const id of ids) {
    const members = groupFields(id, 'terraform').concat(groupFields(id, 'pulumi'))
    assert.ok(members.length > 0, `group ${id} has no fields on either engine`)
  }
})


// ── The #2413 F20 split: engine hides, a gate only disables ────────────────
//
// `terragrunt-version` is the field that exercises both halves at once: it is
// restricted to Terraform AND gated on `terragrunt-enabled`. Collapsing the two
// predicates back together would put dead Terraform controls on every Pulumi
// workspace, which is what these assertions exist to stop.

test('a gated field still APPLIES when its gate is off — the gate only disables', () => {
  // The load-bearing one. If `fieldApplies` ever consults the gate again, this
  // fails, and the form goes back to hiding what it should be disabling.
  assert.equal(fieldApplies('terragrunt-version', 'terraform'), true)
  assert.equal(fieldGateMet('terragrunt-version', {}), false)
  assert.equal(fieldApplies('ansible-version', 'terraform'), true)
  assert.equal(fieldGateMet('ansible-version', {}), false)
})

test('a field belonging to another engine does NOT apply, so it stays hidden', () => {
  assert.equal(fieldApplies('terragrunt-version', 'pulumi'), false)
  assert.equal(fieldApplies('pulumi-bind-plan', 'terraform'), false)
  assert.equal(fieldApplies('pulumi-bind-plan', 'pulumi'), true)
})

test('a gate being ON does not change applicability either way', () => {
  assert.equal(fieldApplies('terragrunt-version', 'terraform'), true)
  assert.equal(fieldGateMet('terragrunt-version', { 'terragrunt-enabled': true }), true)
  assert.equal(fieldApplies('terragrunt-version', 'pulumi'), false)
  assert.equal(fieldGateMet('terragrunt-version', { 'terragrunt-enabled': true }), true)
})

test('both predicates fail closed on an attribute nothing describes', () => {
  // A control nothing declares must not render and must not be editable.
  assert.equal(fieldApplies('no-such-attribute', 'terraform'), false)
  assert.equal(fieldGateMet('no-such-attribute', {}), false)
})

// ---------------------------------------------------------------------------
// The gate reaches a BESPOKE control too (#2413 F20).
//
// `Field` accepts a `control` prop for the editors the primitives cannot
// express -- the per-row path lists, the version datalists, the labels editor,
// the Enabled/Disabled toggles. A bespoke control builds its own `<input>`, so
// `Control`'s `disabled` cannot reach it, and 38 call sites across the two
// forms pass one. That is a RENDER property rather than a pure one, so it is
// asserted against the implementation: the e2e caught the two configure
// triggers rendering under F20 and accepting input, after every pure test
// above passed.
//
// Measured (Playwright, real Chromium, with a positive control): a control
// with no `disabled` of its own inside `<fieldset disabled>` satisfies
// `toBeDisabled()`, while `input.disabled` is `false` -- the IDL property does
// not inherit and the CSS `:disabled` definition does. So a test asserting
// `.disabled` on the element would report the opposite of the truth.

const GROUPS_SRC = readFileSync(
  join(import.meta.dirname, '..', 'src', 'components', 'workspace-field-groups.tsx'),
  'utf8',
)

test('WsField is rendered only by Field, so the gate has no bypass', () => {
  // The whole reason `Field` can be the chokepoint. Reaching for the lower
  // primitive directly would render a label and a control with no gate and no
  // engine check, and nothing else would notice.
  const files = readdirSync(join(import.meta.dirname, '..', 'src'), {
    recursive: true,
    withFileTypes: true,
  })
    .filter((d) => d.isFile() && d.name.endsWith('.tsx'))
    .map((d) => join(d.parentPath, d.name))
  // Line comments stripped first: a comment ABOUT the chokepoint must not
  // fail the gate that guards it, or the lesson is "edit the test".
  const users = files.filter((f) =>
    readFileSync(f, 'utf8')
      .replace(/^\s*\/\/.*$/gm, '')
      .includes('<WsField'),
  )
  assert.deepEqual(
    users.map((f) => f.split('/').pop()),
    ['workspace-field-groups.tsx'],
    'a second WsField caller is a control with no gate -- render it through Field',
  )
})

test('a bespoke control is rendered under the gate, never bare', () => {
  // `{control ?? (<Control .../>)}` is the ungated form this replaces: it
  // hands the caller's own input straight to `WsField`, so `fieldGateMet` is
  // consulted for a primitive and skipped for everything bespoke.
  assert.ok(
    !GROUPS_SRC.includes('{control ?? ('),
    'the bare `control ??` form is back -- a bespoke control now escapes the gate',
  )
  assert.match(
    GROUPS_SRC,
    /\{control \?[\s\S]{0,40}gateMet \?/,
    'the bespoke branch must choose on gateMet',
  )
  assert.match(
    GROUPS_SRC,
    /<fieldset disabled className="min-w-0/,
    'the gated-off branch must wrap in a disabled fieldset, and keep min-w-0: a ' +
      'fieldset defaults to min-inline-size: min-content and will not shrink at phone width',
  )
})

test('the two forms really do pass bespoke controls, so the gate does work', () => {
  // A floor. Without it the assertions above pass on a tree where nothing
  // passes `control` at all, which is the shape that makes a gate decorative.
  const callers = [
    join(import.meta.dirname, '..', 'src', 'app', 'workspaces', 'page.tsx'),
    join(import.meta.dirname, '..', 'src', 'app', 'workspaces', '[id]', 'page.tsx'),
  ]
  const total = callers.reduce(
    (n, f) => n + (readFileSync(f, 'utf8').match(/control=\{/g) ?? []).length,
    0,
  )
  assert.ok(total >= 30, `only ${total} bespoke controls found -- expected the two forms' ~38`)
})
