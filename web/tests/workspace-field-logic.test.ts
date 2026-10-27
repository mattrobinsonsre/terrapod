// The two pieces of real logic behind the shared workspace forms (#2253).
//
// `dependencyMet` decides whether a field is MEANINGFUL — it is what replaced
// the old disable-and-explain idiom, so with drift off the interval input is
// absent rather than rendered showing an 86400 it is not using.
//
// `groupSummary` decides what a COLLAPSED group admits to holding, and it is
// the one design property #2253 says to settle before any markup moves: a
// collapsed group must not conceal a non-default value. Its whole point is
// that it is DERIVED from the value — a count for a list, set/unset for an
// optional scalar — and never from a copy of the model's defaults, because one
// copy of those already exists in `admin/autodiscovery/page.tsx` and a second
// would be the kind of ledger this repo keeps learning drifts.
//
// Neither could be unit-tested while they lived in a file holding JSX, since
// the runner is bare `node --test` with no transpiler. That is why the table
// moved to `src/lib`.
//
// Run with: npm run test:unit

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  FIELD_BY_ATTR,
  FIELDS,
  dependencyMet,
  groupFields,
  groupSummary,
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

test('a counted field surfaces a COUNT while its group is shut — ARRAY shape', () => {
  const held = groupSummary('metadata', 'terraform', { 'var-files': ['a.tfvars', 'b.tfvars'] }, label)
  const vf = held.find((h) => h.attr === 'var-files')
  assert.ok(vf, 'var-files should be summarised')
  assert.match(vf.text, /2/)
})

test('a counted field surfaces a COUNT — MAP shape, which is what labels really is', () => {
  // `labels` is `Mapped[dict[str, Any]]` and `oidc-audiences` is
  // `Mapped[dict[str, list[str]]]`. An earlier version of this test passed an
  // ARRAY for labels -- a shape that never occurs -- so it passed while the
  // implementation reported 0 for every map and a collapsed group holding
  // labels concealed them. The fixture shape is the whole assertion here.
  const held = groupSummary(
    'metadata',
    'terraform',
    { labels: { team: 'platform', tier: 'prod', owner: 'sre' } },
    label,
  )
  const labels = held.find((h) => h.attr === 'labels')
  assert.ok(labels, 'a map-valued field must be summarised, not silently counted as 0')
  assert.match(labels.text, /3/)

  const auds = groupSummary(
    'cloudIdentity',
    'terraform',
    { 'oidc-audiences': { aws: ['sts.amazonaws.com'], azurerm: ['api://AzureADTokenExchange'] } },
    label,
  )
  assert.ok(auds.some((h) => h.attr === 'oidc-audiences'))
  assert.match(auds.find((h) => h.attr === 'oidc-audiences')!.text, /2/)
})

test('an empty collection says nothing, in either shape', () => {
  assert.deepEqual(groupSummary('metadata', 'terraform', { labels: {} }, label), [])
  assert.deepEqual(groupSummary('metadata', 'terraform', { 'var-files': [] }, label), [])
  assert.deepEqual(groupSummary('metadata', 'terraform', {}, label), [])
})

test('an optional scalar surfaces as set, and only when set', () => {
  const set = groupSummary('diagnostics', 'terraform', { 'slack-channel': '#ops' }, label)
  assert.ok(
    set.some((h) => h.attr === 'slack-channel'),
    'a set channel should be announced',
  )
  for (const empty of ['', undefined, null, 0]) {
    assert.deepEqual(
      groupSummary('diagnostics', 'terraform', { 'slack-channel': empty }, label),
      [],
      `an empty channel (${JSON.stringify(empty)}) must not be announced`,
    )
  }
})

test('NO default is ever consulted — a value equal to its default is not announced', () => {
  // The property the whole design rests on. `parallelism` is 10 by default and
  // carries no `summary`, so it cannot be announced at all and nothing here
  // needs to know what 10 means. If a defaults table ever creeps in, the
  // natural first step is giving one of these three a summary — which this
  // asserts is not the case.
  for (const attr of ['parallelism', 'resource-cpu', 'resource-memory']) {
    const spec = FIELD_BY_ATTR[attr]
    assert.ok(spec, `${attr} should be declared`)
    assert.equal(
      spec.summary ?? 'none',
      'none',
      `${attr} has a real default, so it must never be summarised — and must never be collapsed`,
    )
    assert.notEqual(
      spec.advanced,
      true,
      `${attr} has a real default a count cannot express, so it must stay expanded`,
    )
  }
})

test('a summary skips a field whose gate is off', () => {
  // Otherwise a collapsed Ansible group would announce inventory paths on a
  // workspace with configure switched off, i.e. advertise a value that is not
  // in play.
  const off = groupSummary(
    'ansible',
    'terraform',
    { 'configure-enabled': false, 'inventory-paths': ['hosts.d'] },
    label,
  )
  assert.deepEqual(off, [])
  const on = groupSummary(
    'ansible',
    'terraform',
    { 'configure-enabled': true, 'inventory-paths': ['hosts.d'] },
    label,
  )
  assert.ok(on.some((h) => h.attr === 'inventory-paths'))
})

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
