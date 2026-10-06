// What the audience editor puts on the wire, and which provider names it
// refuses before the server has to (#1901).
//
// Both helpers exist because the server REFUSES rather than ignores: a blank
// audience row 422s the save, and so does a provider whose list came out
// empty — removing the key is the defined way to stop overriding one. An empty
// row left behind in the editor would therefore fail a whole fleet update, so
// the client drops it. The interesting half is what must NOT be touched: an
// audience is an opaque string the federation target chose, stored
// byte-for-byte, and normalising it here would make the Terraform provider's
// plan disagree with its own apply.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isValidOidcProvider,
  sanitizeOidcAudiences,
  partitionOidcAudiences,
  sameAudiences,
} from '../src/lib/oidc-audiences.ts'

/* ---------------- sanitizeOidcAudiences ---------------- */

test('an audience is passed through byte-for-byte, never trimmed or folded', () => {
  // The whole point: these are opaque strings chosen elsewhere. Trimming the
  // kept value, lower-casing it, or stripping a trailing slash would store
  // something the operator did not type.
  const out = sanitizeOidcAudiences({
    aws: ['  sts.amazonaws.com  ', 'API://AzureADTokenExchange', 'https://vault.example.com/'],
  })
  assert.deepEqual(out, {
    aws: ['  sts.amazonaws.com  ', 'API://AzureADTokenExchange', 'https://vault.example.com/'],
  })
})

test('a blank audience row is dropped rather than sent', () => {
  // The server refuses a blank entry, so an unfilled row left in the editor
  // would 422 the save for every other provider too.
  const out = sanitizeOidcAudiences({ aws: ['sts.amazonaws.com', '', '   '] })
  assert.deepEqual(out, { aws: ['sts.amazonaws.com'] })
})

test('a provider left with no audiences is dropped with its rows', () => {
  // An explicitly empty list is refused by the server, because it is neither
  // an override nor a removal. Dropping the key IS the removal, and that
  // provider then falls back to the deployment's own audiences.
  const out = sanitizeOidcAudiences({ aws: ['sts.amazonaws.com'], vault: ['', '  '] })
  assert.deepEqual(out, { aws: ['sts.amazonaws.com'] })
})

test('a half-finished entry — a named provider with nothing typed yet — is dropped', () => {
  // The editor seeds a new provider with one blank row so there is somewhere
  // to type, so this is the state of every entry the moment it is added.
  assert.deepEqual(sanitizeOidcAudiences({ vault: [''] }), {})
})

test('an empty map survives as an empty map, which clears every override', () => {
  // Not the same as sending nothing: an empty map is a real value that says
  // "override no provider", and the API distinguishes the two.
  assert.deepEqual(sanitizeOidcAudiences({}), {})
})

test('an aliased key keeps its dot — it is one key, never split or nested', () => {
  // `aws.west` and `aws` are two independent lookups on the server (specific,
  // then general). Splitting on the dot here would silently merge them.
  const out = sanitizeOidcAudiences({
    aws: ['sts.amazonaws.com'],
    'aws.west': ['sts.amazonaws.com'],
  })
  assert.deepEqual(Object.keys(out).sort(), ['aws', 'aws.west'])
})

test('a provider key IS trimmed, because the server refuses any whitespace in one', () => {
  // The only normalisation that happens, and it can only avoid a rejection:
  // whitespace is never part of a provider name.
  assert.deepEqual(sanitizeOidcAudiences({ '  aws  ': ['sts.amazonaws.com'] }), {
    aws: ['sts.amazonaws.com'],
  })
})

test('a key that is nothing but whitespace is dropped', () => {
  assert.deepEqual(sanitizeOidcAudiences({ '   ': ['sts.amazonaws.com'] }), {})
})

test('entry order is preserved, since the audience order is what goes into aud', () => {
  const out = sanitizeOidcAudiences({ vault: ['https://b', 'https://a'] })
  assert.deepEqual(out.vault, ['https://b', 'https://a'])
})

/* ---------------- isValidOidcProvider ---------------- */

test('a bare provider and a single alias are both accepted', () => {
  for (const k of ['aws', 'azurerm', 'google', 'vault', 'aws.west', 'vault.eu']) {
    assert.equal(isValidOidcProvider(k), true, `${k} should be accepted`)
  }
})

test('whitespace, a second dot, and a leading or trailing dot are refused', () => {
  // The same three cheap checks the server applies. Deliberately NOT a
  // grammar: a provider name is whatever the configuration calls it, so
  // inventing a pattern risks refusing a legitimate key.
  for (const k of ['', '   ', 'aws west', 'aws\twest', 'aws.west.two', '.aws', 'aws.']) {
    assert.equal(isValidOidcProvider(k), false, `${k} should be refused`)
  }
})

test('an unfamiliar provider name is accepted — any provider may be mapped', () => {
  // No per-cloud knowledge lives here. The cloud-side trust policy is the gate.
  assert.equal(isValidOidcProvider('acme-internal'), true)
  assert.equal(isValidOidcProvider('kubernetes.staging'), true)
})

/* ---------------- partitionOidcAudiences ---------------- */
//
// The workspace endpoint returns the catalogue with the workspace's own map
// merged over it per key, carrying no marker saying which is which. So the
// only way to show provenance — and the only way to save without promoting
// every inherited entry into an override — is to fetch the catalogue and
// subtract. These pin the subtraction.

test('an entry absent from the catalogue is owned', () => {
  const { owned, fallbacks } = partitionOidcAudiences(
    { vault: ['https://vault.example.com'] },
    { aws: ['sts.amazonaws.com'] },
  )
  assert.deepEqual(owned, { vault: ['https://vault.example.com'] })
  // No fallback for a key the catalogue does not have: removing it makes the
  // key vanish rather than revealing a default.
  assert.deepEqual(fallbacks, {})
})

test('an entry matching the catalogue exactly is INHERITED, not owned', () => {
  // The load-bearing case. Classify this as owned and the next save writes it
  // into the override column, which is the promotion the split exists to stop.
  const { owned, fallbacks } = partitionOidcAudiences(
    { aws: ['sts.amazonaws.com'] },
    { aws: ['sts.amazonaws.com'] },
  )
  assert.deepEqual(owned, {})
  assert.deepEqual(fallbacks, { aws: ['sts.amazonaws.com'] })
})

test('an entry whose list differs from the catalogue is owned', () => {
  const { owned } = partitionOidcAudiences(
    { aws: ['sts.amazonaws.com', 'extra'] },
    { aws: ['sts.amazonaws.com'] },
  )
  assert.deepEqual(owned, { aws: ['sts.amazonaws.com', 'extra'] })
})

test('a REORDER counts as different, so it stays owned', () => {
  // Order is not an implementation detail: the entries are what goes into
  // `aud`, and the server treats a reorder as a change for that reason.
  // Calling it equal here would silently drop a real override.
  const { owned } = partitionOidcAudiences(
    { vault: ['https://b', 'https://a'] },
    { vault: ['https://a', 'https://b'] },
  )
  assert.deepEqual(owned, { vault: ['https://b', 'https://a'] })
})

test('an owned key still reports its fallback, so removing it reveals the default', () => {
  // `fallbacks` deliberately includes keys that are currently owned — it is
  // what each key would fall back TO. The editor filters out the ones present
  // in the owned set, so nothing renders twice, and dropping an owned entry
  // makes the default appear in its place. That is the specified behaviour.
  const { owned, fallbacks } = partitionOidcAudiences(
    { aws: ['mine'] },
    { aws: ['sts.amazonaws.com'] },
  )
  assert.deepEqual(owned, { aws: ['mine'] })
  assert.deepEqual(fallbacks, { aws: ['sts.amazonaws.com'] })
})

test('a catalogue entry the server dropped as malformed is not reported as inherited', () => {
  // It is absent from the effective map, so it is not in force; showing it as
  // inherited would claim otherwise.
  const { owned, fallbacks } = partitionOidcAudiences({}, { aws: ['sts.amazonaws.com'] })
  assert.deepEqual(owned, {})
  assert.deepEqual(fallbacks, {})
})

test('with NO catalogue every entry is owned, which is the fail-soft direction', () => {
  // What a failed defaults probe degrades to: provenance is lost, but what the
  // operator sees is exactly what a save sends, so nothing is silently
  // dropped as inherited.
  const merged = { aws: ['sts.amazonaws.com'], 'aws.west': ['other'] }
  const { owned, fallbacks } = partitionOidcAudiences(merged, {})
  assert.deepEqual(owned, merged)
  assert.deepEqual(fallbacks, {})
})

test('the partition copies, so mutating the result cannot reach the response', () => {
  const merged = { aws: ['sts.amazonaws.com'] }
  const { owned } = partitionOidcAudiences(merged, {})
  owned.aws.push('injected')
  assert.deepEqual(merged, { aws: ['sts.amazonaws.com'] })
})

test('an aliased key is partitioned independently of its bare provider', () => {
  // `aws.west` and `aws` are two separate lookups; one being inherited must
  // say nothing about the other.
  const { owned, fallbacks } = partitionOidcAudiences(
    { aws: ['sts.amazonaws.com'], 'aws.west': ['mine'] },
    { aws: ['sts.amazonaws.com'], 'aws.west': ['theirs'] },
  )
  assert.deepEqual(Object.keys(owned), ['aws.west'])
  assert.deepEqual(Object.keys(fallbacks).sort(), ['aws', 'aws.west'])
})

test('an override equal to the default reads as inherited — the accepted loss', () => {
  // Deliberate and documented: indistinguishable from an inherited entry, so
  // it is dropped from the next save. The effective audiences do not change
  // (the key falls back to the identical default), so the outcome is benign.
  // Same loss AWS's tags/default_tags has lived with for years. Do NOT try to
  // engineer round it.
  const { owned } = partitionOidcAudiences(
    { aws: ['sts.amazonaws.com'] },
    { aws: ['sts.amazonaws.com'] },
  )
  assert.deepEqual(owned, {})
})

/* ---------------- sameAudiences ---------------- */

test('sameAudiences is order-sensitive and length-sensitive', () => {
  assert.equal(sameAudiences(['a', 'b'], ['a', 'b']), true)
  assert.equal(sameAudiences(['a', 'b'], ['b', 'a']), false)
  assert.equal(sameAudiences(['a'], ['a', 'b']), false)
  assert.equal(sameAudiences([], []), true)
})
