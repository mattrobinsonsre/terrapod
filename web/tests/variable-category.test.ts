// Which label the one native variable category wears, per engine (#1898).
//
// There is a single stored category for every engine's own parameters, but the
// UI names it in the words of the engine the operator is looking at: a Pulumi
// workspace offering "Terraform" was the confusion this change set out to
// remove, and naming the neutral value at a Terraform operator replaces one
// piece of jargon with another.
//
// This exists because the keys are built at RUNTIME. `check-i18n-keys-resolve`
// deliberately skips a key it cannot read as a literal — a sound choice, and it
// means these three are the only labels in the app no gate would notice going
// missing. Its own docstring records that exactly this gap shipped a namespace
// typo (#1334) that every gate passed and a human found at render. So the
// resolution is checked here instead, against the real catalogue.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { categoryKey, nativeCategoryKey } from '../src/lib/variable-category.ts'

const en = JSON.parse(readFileSync('messages/en.json', 'utf8'))
const VARIABLES = en.workspaceDetail.variables
const VARSET = en.adminVariableSets.detail

test('a Terraform workspace keeps the name it always had', () => {
  assert.equal(nativeCategoryKey('terraform'), 'categoryTerraform')
  assert.equal(VARIABLES.categoryTerraform, 'Terraform')
})

test('a Pulumi workspace is named in its own words, never Terraform', () => {
  assert.equal(nativeCategoryKey('pulumi'), 'categoryPulumiConfig')
  assert.equal(VARIABLES.categoryPulumiConfig, 'Pulumi config')
})

test('the neutral name appears only where there is no engine to name', () => {
  // A variable set is org-scoped and reaches workspaces of either kind, so no
  // engine's word for the category would be right.
  assert.equal(nativeCategoryKey(undefined), 'categoryNative')
  assert.equal(nativeCategoryKey(null), 'categoryNative')
  assert.equal(nativeCategoryKey(''), 'categoryNative')
  assert.equal(VARIABLES.categoryNative, 'Native')
  assert.equal(VARSET.categoryNative, 'Native')
})

test('an unset or unknown engine reads as Terraform, as the server treats it', () => {
  assert.equal(nativeCategoryKey('tofu'), 'categoryTerraform')
  assert.equal(nativeCategoryKey('ansible'), 'categoryTerraform')
})

test('every key the helper can return resolves in the catalogue', () => {
  // The whole point: these are built at runtime, so nothing else checks them.
  for (const engine of [undefined, null, '', 'terraform', 'tofu', 'pulumi', 'nonsense']) {
    const key = nativeCategoryKey(engine)
    assert.equal(typeof VARIABLES[key], 'string', `workspaceDetail.variables.${key} is missing`)
  }
})

test('env keeps one name on every engine; a git credential is not translated', () => {
  // `env` reaches the process environment whatever runs, so it does not change
  // with the engine. The git pair names a protocol, not a per-engine concept.
  for (const engine of ['terraform', 'pulumi', undefined]) {
    assert.equal(categoryKey('env', engine), 'categoryEnv')
    assert.equal(categoryKey('git_http_auth', engine), null)
    assert.equal(categoryKey('git_ssh_auth', engine), null)
  }
  assert.equal(typeof VARIABLES.categoryEnv, 'string')
})

test('the native category takes the engine label through categoryKey too', () => {
  assert.equal(categoryKey('native', 'terraform'), 'categoryTerraform')
  assert.equal(categoryKey('native', 'pulumi'), 'categoryPulumiConfig')
  assert.equal(categoryKey('native'), 'categoryNative')
})

test('every locale carries all three labels, not just the source', () => {
  // Parity is checked by check-i18n-completeness, but these three are the ones
  // a runtime key would silently fall through on, so pin them directly.
  const locales = ['de', 'fr', 'ja', 'tlh', 'en-x-leet', 'en-x-pirate']
  for (const loc of locales) {
    const cat = JSON.parse(readFileSync(`messages/${loc}.json`, 'utf8'))
    for (const key of ['categoryTerraform', 'categoryPulumiConfig', 'categoryNative']) {
      assert.equal(typeof cat.workspaceDetail.variables[key], 'string', `${loc}: ${key}`)
    }
  }
})

test('every edit panel on the workspace page is told the engine', () => {
  // Desktop and mobile render SEPARATE panels, and a prop added to one and not
  // the other is invisible: the page compiles, the desktop view is right, and
  // the phone view quietly names the wrong engine. That is exactly what
  // happened here — the mobile panel kept saying "Native" on a Pulumi
  // workspace until a browser was pointed at it — so the guard is on the count,
  // not on one call site.
  const src = readFileSync('src/app/workspaces/[id]/page.tsx', 'utf8')
  const panels = src.match(/<VariableEditPanel\b/g) ?? []
  assert.ok(panels.length >= 2, 'expected a desktop and a mobile panel')

  // Each opening tag must carry engine= before its closing bracket.
  const withEngine = src.match(/<VariableEditPanel\b[^>]*\bengine=/g) ?? []
  assert.equal(
    withEngine.length,
    panels.length,
    `${panels.length - withEngine.length} <VariableEditPanel> on the workspace page has no engine prop — ` +
      'it will show the neutral label instead of the engine\'s own word',
  )
})

test('both viewports label the category the same way', () => {
  // The same miss in the other tree: the list rows render twice, a table on
  // desktop and cards on mobile, and each has its own category pill.
  const src = readFileSync('src/app/workspaces/[id]/page.tsx', 'utf8')
  const labelled = src.match(/categoryLabel\(v\.attributes\.category, attrs\.engine\)/g) ?? []
  assert.equal(labelled.length, 2, 'expected the desktop table and the mobile card to both use the engine label')
  assert.ok(
    !/\{v\.attributes\.category\}/.test(src),
    'a raw category value is being rendered — it should go through categoryLabel',
  )
})

test('the variable-set page never passes an engine', () => {
  // A set is org-scoped. Passing any engine there would name one arbitrarily.
  const src = readFileSync('src/app/admin/variable-sets/[id]/page.tsx', 'utf8')
  assert.ok(!/<VariableEditPanel\b[^>]*\bengine=/.test(src), 'a variable set has no single engine to name')
  const labelled = src.match(/categoryLabel\(v\.attributes\.category\)/g) ?? []
  assert.equal(labelled.length, 2, 'expected both viewports to label the category')
})
