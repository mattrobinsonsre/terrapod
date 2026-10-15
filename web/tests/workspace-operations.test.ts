// A workspace declares its operations with TWO BOOLEANS, and `config-directory`
// is just a path (#1966).
//
// This file replaces the tri-state guards #1929 shipped. Those existed because
// `config-directory` was nullable and its NULL carried a meaning — "this
// workspace has no configure operation" — so every falsy-collapsing idiom the
// rest of the page uses legitimately for `working-directory` was a defect on it.
// The column is now non-nullable with `''` meaning the configuration version's
// root, exactly `working-directory`'s shape, and the declaration moved to
// `configure-enabled`. So the collapse those guards prevented is no longer
// possible, and three NEW mistakes are:
//
//   1. Sending `config-directory: null`. It used to be the clear; it is now a
//      422 naming the field, so any UI that still sends it is broken.
//   2. Keying the Inventory tab's gate on `config-directory`. That column is
//      now present on EVERY workspace, so the old `dir != null` test became
//      true for all of them — an Inventory tab on every terraform-only
//      workspace in the deployment. An emptiness test is wrong the other way:
//      `''` is the ordinary way to say "at the root". The gate is the boolean,
//      which is what the server's own `has_anything` keys on.
//   3. Seeding either boolean with `||`. A stored `false` is a value, and `||`
//      reads it as absent and substitutes the column default — so the editor
//      would offer to turn back on exactly the operation the operator turned
//      off.
//
// Source-introspection, because every one of the three is invisible to a
// behavioural test driven by the fixtures that exist: a workspace with
// `plan-apply-enabled: true` and a concrete config directory renders and
// round-trips identically whether or not any of these is right.
//
// `assert.ok(!re.test(...))` rather than `assert.doesNotMatch`, because the
// latter prints the whole 280 KB source as `actual` on failure.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (...parts: string[]) => readFileSync(path.join(here, '..', ...parts), 'utf8')

const page = read('src', 'app', 'workspaces', '[id]', 'page.tsx')
const panel = read('src', 'components', 'inventory-panel.tsx')
const bulk = read('src', 'app', 'admin', 'bulk-update', 'page.tsx')
const autodiscovery = read('src', 'app', 'admin', 'autodiscovery', 'page.tsx')

/** Every surface that writes a workspace or the rule that materialises one. */
const WRITERS = [
  ['workspaces/[id]/page.tsx', page],
  ['admin/bulk-update/page.tsx', bulk],
  ['admin/autodiscovery/page.tsx', autodiscovery],
] as const

test('no surface sends config-directory: null — the server answers 422', () => {
  // The whole point of #1966: an explicit null is REFUSED and naming the field,
  // never coerced to ''. A server that rewrote a submitted value would make a
  // provider's plan disagree with its own result, so the UI must not rely on it.
  for (const [name, src] of WRITERS) {
    assert.ok(
      !/'config-directory':[^,\n]*\bnull\b/.test(src),
      `${name} still sends a null config-directory, which is now a 422`,
    )
  }
})

test('the "no configure operation" affordance is gone from every surface', () => {
  // It named a state the column no longer has. Leaving the option in place
  // would offer an operator a value the server refuses, and leaving the message
  // key in place would have the same words describe nothing.
  for (const [name, src] of WRITERS) {
    assert.ok(
      !/configDirectoryModeNone|configDirectoryModeSet/.test(src),
      `${name} still offers the retired config-directory mode`,
    )
    assert.ok(
      !/[Cc]onfigDirectoryMode\b/.test(src),
      `${name} still carries the retired mode state`,
    )
  }
})

test('both booleans reach the two fleet surfaces the parity gate checks', () => {
  // `test_workspace_setting_parity.py` greps these two files for the attribute
  // names, so this is the same property asserted on the side that can run in
  // the web test tier — and, unlike that gate, it also requires a control
  // rather than merely the string.
  for (const [name, src] of [
    ['admin/bulk-update/page.tsx', bulk],
    ['admin/autodiscovery/page.tsx', autodiscovery],
  ] as const) {
    for (const attr of ['plan-apply-enabled', 'configure-enabled']) {
      assert.ok(src.includes(`'${attr}'`), `${name} does not write ${attr}`)
    }
    // A control, not just a payload key: the id is what the label points at.
    assert.match(src, /id="(bu|ad)-plan-apply-enabled"/, `${name} has no plan/apply control`)
    assert.match(src, /id="(bu|ad)-configure-enabled"/, `${name} has no configure control`)
  }
})

test('the workspace editor seeds both booleans with ?? so a stored false survives', () => {
  // `||` would read a stored `false` as absent and substitute the column
  // default — which for plan-apply-enabled means offering to turn back on the
  // operation the operator just turned off.
  assert.match(
    page,
    /setEditPlanApplyEnabled\(workspace\.attributes\['plan-apply-enabled'\] \?\? true\)/,
  )
  assert.match(
    page,
    /setEditConfigureEnabled\(workspace\.attributes\['configure-enabled'\] \?\? false\)/,
  )
  for (const attr of ['plan-apply-enabled', 'configure-enabled']) {
    assert.ok(
      !new RegExp(`\\['${attr}'\\]\\s*\\|\\|`).test(page),
      `${attr} must not be seeded with ||, which loses a stored false`,
    )
  }
  // The rule form has the same hazard for the same reason.
  assert.match(autodiscovery, /a\['plan-apply-enabled'\] \?\? true/)
  assert.match(autodiscovery, /a\['configure-enabled'\] \?\? false/)
})

test('the config directory is sent as typed, so the server can refuse it', () => {
  // The server REFUSES a path carrying leading or trailing whitespace, a
  // leading or trailing slash, or `..` rather than normalising it — so
  // normalising here would hide a 422 the operator needs to read, and would
  // store something other than what they typed.
  assert.match(page, /'config-directory': editConfigDir,/)
  assert.ok(
    !/editConfigDir\.trim\(\)/.test(page),
    'the config directory must be sent as typed, so the server can refuse it',
  )
})

test('the read-only render has TWO states, and the empty one says "root"', () => {
  // Falling back to `/` the way `working-directory` does would be true but
  // illegible for a path whose being empty is a deliberate choice.
  assert.match(page, /t\('fields\.configDirectoryRoot'\)/)
  assert.ok(
    !/attrs\['config-directory'\] === null/.test(page),
    'the read no longer has a null state to test for',
  )
})

test('both operation booleans are offered on the workspace editor', () => {
  assert.match(page, /id="ws-plan-apply-enabled"/)
  assert.match(page, /id="ws-configure-enabled"/)
  assert.match(page, /t\('fields\.planApplyEnabled'\)/)
  assert.match(page, /t\('fields\.configureEnabled'\)/)
})

test('every surface blocks the both-false save the server refuses', () => {
  // A workspace with neither operation can never produce a run, so the server
  // refuses the pair that survives the write. Blocking it here is what turns a
  // 422-after-save into a sentence beside the controls.
  // COUNTED, not merely present. Two of these three pages gate several
  // controls on the same flag, so `assert.match` is satisfied while one of
  // them has lost the guard — a presence check where an application check is
  // wanted. Found by mutating a single call site and watching this pass.
  const occurrences = (src: string, re: RegExp) => (src.match(re) ?? []).length

  assert.match(page, /editHasNoOperations = !editPlanApplyEnabled && !editConfigureEnabled/)
  // One guarded control: Save. (Cancel must stay reachable — an operator who
  // ticked both off needs a way out that is not a save.)
  assert.equal(occurrences(page, /disabled=\{saving \|\| editHasNoOperations\}/g), 1)

  assert.match(autodiscovery, /ruleHasNoOperations = !planApplyEnabled && !configureEnabled/)
  // TWO guarded controls: the submit and the preview. A preview of a rule that
  // cannot be saved is a plan for workspaces nobody can create.
  assert.equal(occurrences(autodiscovery, /disabled=\{submitting \|\| ruleHasNoOperations\}/g), 2)

  // The fleet form blocks only BOTH explicitly false: turning plan/apply off
  // alone is legitimate across a set that already has configure on, and the
  // endpoint answers that against each matched row.
  assert.match(
    bulk,
    /uWouldLeaveNoOperations = uPlanApplyEnabled === 'false' && uConfigureEnabled === 'false'/,
  )
  // THREE guarded controls: the dry-run preview, Apply, and Confirm apply.
  // Leaving any one unguarded is a path straight to the 422.
  assert.equal(occurrences(bulk, /disabled=\{submitting \|\| uWouldLeaveNoOperations\}/g), 3)
})

test('the inventory tab gate keys on configure-enabled, not on the path', () => {
  // THE regression this file exists for. `config-directory` is now present on
  // every workspace, so the old `dir != null` test opened the tab for all of
  // them; and an emptiness test on it would shut the tab for every workspace
  // whose configure operation runs at the root, which is the common case.
  assert.match(panel, /\['configure-enabled'\] === true/)
  // Anchored on the SUBSCRIPT, not on the bare attribute name: the module's own
  // docstring names `config-directory` to record why it is no longer the gate,
  // and a name-only matcher would be satisfied by that prose — the shape of
  // matcher that reports a gap as closed because a comment mentions it.
  assert.ok(
    !/\['config-directory'\]/.test(panel),
    'the gate must not READ config-directory — it is true for every workspace now',
  )
})

test('the inventory tab gate reads the workspace off the native prefix', () => {
  // `/api/tfe/v2` is engine-filtered to Terraform and drops these attributes,
  // so the compatibility prefix would answer 200 with the key missing — a gate
  // that silently never opens.
  assert.match(panel, /apiFetch\(`\/api\/v1\/workspaces\/\$\{workspaceId\}`\)/)
  assert.ok(
    !/\/api\/(tfe\/)?v2\//.test(panel),
    'the inventory panel must not reach the TFE compatibility surface',
  )
})

test('the retired inventory settings surface is gone', () => {
  for (const [name, src] of [
    ['page.tsx', page],
    ['inventory-panel.tsx', panel],
  ] as const) {
    assert.ok(!/inventory\/settings/.test(src), `${name} still calls the deleted route`)
    assert.ok(!/SettingsView/.test(src), `${name} still references the settings view`)
  }
  // Scoped to the panel: its `t` is bound to `workspaceDetail.inventory`, so a
  // bare `t('settings.…')` there reads a deleted key. The page has its own,
  // unrelated `workspaceDetail.settings` namespace for the Configuration card.
  assert.ok(
    !/\bt\(['"`]settings\./.test(panel),
    'the inventory panel still reads a deleted inventory.settings.* key',
  )
  assert.ok(
    !/\bnav\.settings\b|'settings'/.test(panel),
    "the inventory panel still offers a 'settings' sub-view",
  )
})
