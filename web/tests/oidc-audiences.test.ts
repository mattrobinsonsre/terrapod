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
