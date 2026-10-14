// `config-directory` has THREE states on the workspace Configuration tab, and
// two of them are falsy (#1929).
//
// `null` means this workspace has no configure operation at all; `''` means the
// configure operation's root is the top of the repository; anything else is a
// directory. The server keeps the first two apart deliberately — it serialises
// the column as `null` rather than `""` for exactly that reason — so every
// falsy-collapsing idiom the rest of this page uses legitimately for
// `working-directory` (`|| ''`, a bare truthiness test) is a defect here: it
// would silently give every workspace a configure operation rooted at the top
// of its repository.
//
// These are source-introspection guards because the collapse is invisible to a
// behavioural test: a fixture carrying a concrete directory renders and
// round-trips identically either way, and the only fixture that can tell the
// two apart is one storing the empty string.
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

test('seeding the editor keeps null and the empty string apart', () => {
  // `|| ''` would read a stored '' as "no configure operation", so the next
  // save would clear a workspace whose root IS the repository root.
  assert.ok(
    !/\['config-directory'\]\s*\|\|\s*''/.test(page),
    "config-directory must not be seeded with `|| ''`",
  )
  assert.match(
    page,
    /cfgDir === undefined \|\| cfgDir === null \? 'none' : 'set'/,
    'the mode must be decided by an explicit null/undefined test',
  )
})

test('the save sends null for "no configure operation" and the box verbatim otherwise', () => {
  assert.match(
    page,
    /'config-directory': editConfigDirMode === 'set' \? editConfigDir : null,/,
    "the PATCH must carry null in 'none' mode and the untrimmed box in 'set' mode",
  )
  // The server REFUSES a path carrying leading or trailing whitespace, a
  // leading or trailing slash, or `..` rather than normalising it — so
  // normalising here would hide a 422 the operator needs to read, and would
  // store something other than what they typed.
  assert.ok(
    !/editConfigDir\.trim\(\)/.test(page),
    'the config directory must be sent as typed, so the server can refuse it',
  )
})

test('the read-only render distinguishes all three states', () => {
  assert.match(
    page,
    /attrs\['config-directory'\] === ''/,
    'the empty string must render as the repository root, not as absent',
  )
  assert.match(page, /t\('fields\.configDirectoryRoot'\)/)
  assert.match(page, /t\('fields\.configDirectoryModeNone'\)/)
})

test('the inventory tab gate reads config-directory off the native prefix', () => {
  // `/api/tfe/v2` is engine-filtered to Terraform and drops this attribute, so
  // the compatibility prefix would answer 200 with the key missing — a gate
  // that silently never opens on the config-directory-only case.
  assert.match(panel, /apiFetch\(`\/api\/v1\/workspaces\/\$\{workspaceId\}`\)/)
  assert.ok(
    !/\/api\/(tfe\/)?v2\//.test(panel),
    'the inventory panel must not reach the TFE compatibility surface',
  )
})

test('the gate tests config-directory against null, not for truthiness', () => {
  // `if (dir)` would shut the gate on '' — a workspace whose config root is the
  // repository root, which IS a configure operation and must open the tab.
  assert.match(panel, /return dir != null/)
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
