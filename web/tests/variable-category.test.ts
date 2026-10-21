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

// ---------------------------------------------------------------------------
// Ansible credential categories (#1933)
// ---------------------------------------------------------------------------

// The credential FIELDS live in their own module beside `vault-reference-fields`
// -- the same kind of thing, a shared field group for a variable whose value is
// a structured envelope. The panel itself is read separately below, because two
// of the assertions here are about the panel's own behaviour (what it offers for
// a credential category) rather than about the fields.
const FIELDS = readFileSync('src/components/ansible-credential-fields.tsx', 'utf8')
const PANEL_SRC = readFileSync('src/components/variable-edit-panel.tsx', 'utf8')
// Both, for the assertions that do not care which file a thing is in.
const PANEL = FIELDS + PANEL_SRC
const WS_PAGE = readFileSync('src/app/workspaces/[id]/page.tsx', 'utf8')
const VARSET_PAGE = readFileSync('src/app/admin/variable-sets/[id]/page.tsx', 'utf8')
/** The server's own definition of the two envelopes — see the gate below. */
const SERVER_AUTH = readFileSync('../services/terrapod/services/ansible_auth.py', 'utf8')
const EN_ANSIBLE: Record<string, string> = en.ansibleCredentials

/**
 * Where `NAME` is ASSIGNED, not where it is first mentioned.
 *
 * `indexOf(name)` finds the module docstring, which names these sets in prose —
 * and the next `{` after that prose is the Ansible Vault section's
 * `{"password": …}` example. So `MACHINE_FILE_FIELDS` read back as `password`
 * rather than `private_key`: a gate quietly asserting the panel writes the
 * wrong field. Anchor on the assignment so the region is the definition.
 */
function pythonAssignment(source: string, name: string): number {
  const at = source.search(new RegExp(`^${name}(?::[^=\\n]*)? = `, 'm'))
  assert.notEqual(at, -1, `${name} is not assigned in ansible_auth.py`)
  return at
}

/** The `"key"` literals inside one `NAME = frozenset({ … })` block. */
function pythonSetMembers(source: string, name: string): string[] {
  const start = pythonAssignment(source, name)
  const open = source.indexOf('{', start)
  const close = source.indexOf('}', open)
  assert.ok(open !== -1 && close > open, `could not read the ${name} block`)
  return [...source.slice(open, close).matchAll(/"([a-z_]+)"/g)].map((m) => m[1]).sort()
}

/**
 * The envelope fields a slice of the panel writes.
 *
 * `secret(...)` and `text(...)` are the two field renderers, and `set(...)` is
 * the writer the `become` select calls directly — between them they are every
 * field the form can put in the envelope.
 */
function formFields(slice: string): string[] {
  const names = [...slice.matchAll(/\b(?:secret|text|set)\('([a-z_]+)'/g)].map((m) => m[1])
  return [...new Set(names)].sort()
}

/** The panel split into its vault branch and its machine branch. */
function panelBranches(): { vault: string; machine: string } {
  const vaultMark = 'if (category === ANSIBLE_VAULT_CATEGORY) {'
  const machineMark = "{t('machineHint')}"
  // A non-unique anchor would slice at the wrong place and the assertions below
  // would then be about a region nobody chose.
  assert.equal(PANEL.split(vaultMark).length - 1, 1, 'the vault-branch anchor is not unique')
  assert.equal(PANEL.split(machineMark).length - 1, 1, 'the machine-branch anchor is not unique')
  const vaultAt = PANEL.indexOf(vaultMark)
  const machineAt = PANEL.indexOf(machineMark)
  assert.ok(vaultAt < machineAt, 'the vault branch is expected before the machine branch')
  return { vault: PANEL.slice(vaultAt, machineAt), machine: PANEL.slice(machineAt) }
}

test('an Ansible credential category has no translated pill, as a git one has none', () => {
  // `categoryKey` returns a message key only where the label is translated, and
  // the two credential pairs are the categories where it is not: a list row
  // shows the stored value. The option in the picker IS translated — that is a
  // different label, built in the component, and this answers the pill.
  for (const engine of ['terraform', 'pulumi', undefined]) {
    assert.equal(categoryKey('ansible_machine_auth', engine), null)
    assert.equal(categoryKey('ansible_vault_auth', engine), null)
  }
})

test('the machine form writes exactly the fields the server accepts', () => {
  // Why the server keeps those names in one file: "a disagreement about a
  // single key — `become_pass` against `become_password` — is invisible. The
  // API accepts the credential, the runner finds nothing under the name it
  // expects, and the configure runs with no escalation and no error." Nothing
  // but this compares the two sides.
  //
  // Both are `frozenset({…})`, so they are read with the SET helper. They were
  // read with a dict-key helper matching `"x":`, which a frozenset has none of,
  // so the gate could only ever fail — see `pythonAssignment` for the second
  // half of why it could not pass.
  const expected = [
    ...pythonSetMembers(SERVER_AUTH, 'MACHINE_FILE_FIELDS'),
    ...pythonSetMembers(SERVER_AUTH, 'MACHINE_VALUE_FIELDS'),
  ].sort()
  assert.ok(expected.length >= 7, `read only ${expected.length} machine fields from the server`)
  assert.ok(expected.includes('private_key'), 'the file-delivered field should be among them')
  assert.deepEqual(formFields(panelBranches().machine), expected)
})

test('the vault form writes exactly the one field the server accepts', () => {
  const expected = pythonSetMembers(SERVER_AUTH, 'VAULT_FIELDS')
  assert.deepEqual(expected, ['password'])
  assert.deepEqual(formFields(panelBranches().vault), expected)
})

test('every field the server calls secret is entered through the masked input', () => {
  // A plain text input for `become_password` puts a secret on screen in
  // readable form, which no behavioural test would notice.
  const expected = pythonSetMembers(SERVER_AUTH, 'MACHINE_SECRET_FIELDS')
  assert.deepEqual(expected, ['become_password', 'password', 'private_key'])
  const machine = panelBranches().machine
  const names = [...machine.matchAll(/\bsecret\('([a-z_]+)'/g)].map((m) => m[1])
  assert.deepEqual([...new Set(names)].sort(), expected)
})

test('both categories are offered on all three surfaces that write a variable', () => {
  // A category offered in the workspace form and not in the variable-set form
  // is a credential an operator can set in one place and not the other, which
  // reads as the feature being broken. The set form is the one that matters
  // most here: one vault password shared across the workspaces that need it.
  const surfaces: [string, string][] = [
    ['the edit panel', PANEL],
    ['the workspace page', WS_PAGE],
    ['the variable-set page', VARSET_PAGE],
  ]
  for (const [name, src] of surfaces) {
    assert.match(src, /<option value=\{ANSIBLE_MACHINE_CATEGORY\}>/, `${name} does not offer the machine category`)
    assert.match(src, /<option value=\{ANSIBLE_VAULT_CATEGORY\}>/, `${name} does not offer the vault category`)
    assert.match(src, /<AnsibleCredentialFields\b/, `${name} has no credential builder`)
  }
})

test('no surface falls back to a raw value box for an Ansible credential', () => {
  // The regression the git builder already avoided: the operator hand-writes a
  // JSON envelope whose field names only the server knows. Each page's plain
  // value field has to be gated off for these categories, and the panel has to
  // choose the builder over its own value box.
  assert.match(
    WS_PAGE,
    /!isGitCat && !isAnsibleCat && !isVaultSource &&/,
    'the workspace page still shows its raw value field for an Ansible credential',
  )
  assert.match(
    VARSET_PAGE,
    /\) : isAnsibleCat \? \(/,
    'the variable-set page still shows its raw value field for an Ansible credential',
  )
  assert.match(
    PANEL,
    /\) : isAnsibleCat \? \(\s*<AnsibleCredentialFields/,
    'the edit panel still shows its raw value field for an Ansible credential',
  )
})

test('neither category can be given a Vault value source on any surface', () => {
  // The API refuses value-source `vault` on an envelope category with a 422
  // (#1439/#1933), so offering the source is a form that cannot be saved.
  assert.match(PANEL, /vaultAvailable && !isCredentialCategory\(state\.category\)/)
  assert.match(WS_PAGE, /varSource === 'vault' && !isGitCat && !isAnsibleCat/)
  assert.match(VARSET_PAGE, /varSource === 'vault' && !isGitCat && !isAnsibleCat/)
})

test('every offered locale carries the whole Ansible credential namespace', () => {
  // `check-i18n-completeness` proves parity for every locale, which covers this
  // — except `en-GB`, which it checks as a subset. So state here which locales
  // are expected to carry the block at all, and that it is complete wherever it
  // is carried.
  const config = readFileSync('src/i18n/config.ts', 'utf8')
  const block = config.match(/export const locales = \[([\s\S]*?)\] as const/)
  assert.ok(block, 'could not find the `locales` array in src/i18n/config.ts')
  const offered = [...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1])
  const keys = Object.keys(EN_ANSIBLE)
  assert.ok(keys.length >= 19, `expected the full namespace, read ${keys.length} keys`)
  for (const loc of offered) {
    const cat = JSON.parse(readFileSync(`messages/${loc}.json`, 'utf8'))
    if (loc === 'en-GB') {
      // A dialect override carries only the spelling deltas, and none of these
      // strings differs between British and American English.
      assert.equal(cat.ansibleCredentials, undefined, 'en-GB should carry no deltas here')
      continue
    }
    for (const key of keys) {
      const value = cat.ansibleCredentials ? cat.ansibleCredentials[key] : undefined
      assert.equal(typeof value, 'string', `${loc}: ansibleCredentials.${key} is missing`)
    }
  }
})

test('the novelty locales genuinely transform the credential copy', () => {
  // The completeness gate passes on a catalogue holding the English string, so
  // a joke locale left untranslated is invisible to it — which is how the git
  // credential block came to be verbatim English in all thirty-one locales
  // that carry it (#2198; `en` is the source and `en-GB` carries no delta, so
  // thirty-one, not thirty-three). It is translated now, and
  // `git-credential-copy.test.ts` holds it there.
  //
  // Each exemption names the string and the reason. They are the two shapes
  // that legitimately do not move: a word the transform does not touch
  // (marklar swaps NOUNS only), and a single word with no clause to invert.
  //
  // `scope` was once exempt for yoda, as a bare one-word label with no clause
  // to reorder. The key is now `name` and the label `Credential name`, a
  // two-word noun phrase -- which yoda DOES move, by the catalogue's own
  // standing convention for exactly this shape (`vaultLabel` is
  // `Label, Vault ID`; `becomeMethod` is `Method, escalation`). So the
  // exemption is gone rather than carried over, and the test that backed it
  // went with it.
  const exempt: Record<string, Record<string, string>> = {
    'en-x-marklar': {
      becomeUnset: 'adjectival — marklar replaces nouns, not adjectives',
      becomeYes: 'adjectival — marklar replaces nouns, not adjectives',
      becomeNo: 'adjectival — marklar replaces nouns, not adjectives',
    },
    'en-x-yoda': {
      password: 'one word; there is no clause to reorder',
      user: 'one word; there is no clause to reorder',
    },
  }
  const novelty = ['tlh', 'en-x-marklar', 'en-x-lolcat', 'en-x-leet', 'en-x-pirate', 'en-x-yoda']
  for (const loc of novelty) {
    const cat = JSON.parse(readFileSync(`messages/${loc}.json`, 'utf8'))
    for (const key of Object.keys(EN_ANSIBLE)) {
      const same = cat.ansibleCredentials[key] === EN_ANSIBLE[key]
      const reason = exempt[loc] ? exempt[loc][key] : undefined
      if (reason) {
        assert.ok(same, `${loc}: ${key} is exempt as "${reason}" but has moved — drop the exemption`)
      } else {
        assert.ok(!same, `${loc}: ${key} is still the English string`)
      }
    }
  }
})

test('the yoda credential name follows the catalogue own standing convention', () => {
  // The exemption list above drops `scope` on the ground that yoda moves a
  // two-word noun phrase. That is a claim about the catalogue, so it is
  // asserted rather than trusted — and it is what makes `Name, credential` the
  // expected shape rather than one translator's choice.
  const yoda = JSON.parse(readFileSync('messages/en-x-yoda.json', 'utf8')).ansibleCredentials
  assert.equal(yoda.vaultLabel, 'Label, Vault ID')
  assert.equal(yoda.becomeMethod, 'Method, escalation')
  assert.equal(yoda.name, 'Name, credential')
})

test('en-x-leet applies its own substitution and leaves a typed-in value alone', () => {
  // The locale is a deterministic character substitution, so it is checked
  // against the rule rather than against a snapshot of itself. Two things the
  // existing catalogue does not substitute: an ICU placeholder name, and the
  // content of a <code> span. A value the operator types verbatim is the same
  // class — which is why the credential placeholder and the example name it
  // interpolates survive in the name hint. `7errap0d` there would be actively
  // wrong: the operator writes that string into an inventory. Leet-ing one
  // occurrence of `web-key` and not the one inside the placeholder would break
  // the correspondence the example exists to show, so both are preserved.
  const verbatim = String.raw`\$'\{'terrapod:[^'{}]*'\}'|web-key`
  const map: Record<string, string> = { a: '4', e: '3', i: '1', o: '0', s: '5', t: '7' }
  const leetify = (s: string) =>
    [...s].map((ch) => map[ch] || map[ch.toLowerCase()] || ch).join('')
  /**
   * Leetify everything except the spans an operator types verbatim. Split on a
   * CAPTURING group, so the delimiters land at the odd indices and are rejoined
   * untouched.
   */
  const leetSource = (s: string) =>
    s
      .split(new RegExp(`(${verbatim})`))
      .map((part, i) => (i % 2 ? part : leetify(part)))
      .join('')
  const cat = JSON.parse(readFileSync('messages/en-x-leet.json', 'utf8')).ansibleCredentials
  for (const key of Object.keys(EN_ANSIBLE)) {
    assert.equal(
      cat[key],
      leetSource(EN_ANSIBLE[key]),
      `en-x-leet.${key} is not the substitution of the source`,
    )
  }
  // The guard only means something if the source still carries a verbatim span
  // for it to preserve — otherwise it is satisfied by a plain substitution.
  assert.match(EN_ANSIBLE.nameHint, new RegExp(verbatim), 'the source should carry a verbatim span')
  assert.ok(
    cat.nameHint.includes("$'{'terrapod:web-key.private_key'}'"),
    'the placeholder an operator types should survive',
  )
})

test('the two category values are the server constants, character for character', () => {
  // The rest of this block checks the envelope's field names against the
  // server; the category is the other half of the same contract, and a typo in
  // it makes every write 422 on a value nothing in the UI displays.
  const server = Object.fromEntries(
    [...SERVER_AUTH.matchAll(/^(MACHINE_CATEGORY|VAULT_CATEGORY) = "([a-z_]+)"$/gm)]
      .map((m) => [m[1], m[2]]),
  )
  assert.deepEqual(Object.keys(server).sort(), ['MACHINE_CATEGORY', 'VAULT_CATEGORY'])
  assert.match(
    PANEL,
    new RegExp(`ANSIBLE_MACHINE_CATEGORY = '${server.MACHINE_CATEGORY}'`),
    'the machine category does not match the server constant',
  )
  assert.match(
    PANEL,
    new RegExp(`ANSIBLE_VAULT_CATEGORY = '${server.VAULT_CATEGORY}'`),
    'the vault category does not match the server constant',
  )
  // And the helper in src/lib must agree, since the list rows read it.
  assert.equal(categoryKey(server.MACHINE_CATEGORY, 'terraform'), null)
  assert.equal(categoryKey(server.VAULT_CATEGORY, 'terraform'), null)
})
