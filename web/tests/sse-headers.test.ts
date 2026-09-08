// Every SSE endpoint gets the passthrough header, on BOTH API prefixes (#1529).
//
// The BFF must set `Content-Encoding: none` on SSE paths or Next compresses and
// buffers the stream and events never reach the browser. That failure is silent:
// the connection opens, the response is a 200, and the page simply stops
// updating. Nothing in the Python suite can see it, and an E2E test of it is
// awkward because an SSE response never completes — so this asserts the config
// directly, which is where the mistake would actually be made.
//
// The list is derived (prefix × path) rather than written out twice, so the real
// regression this guards is someone adding an SSE endpoint to one prefix and
// forgetting the other.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'

// next.config.js is CommonJS; this file is loaded as ESM.
const require = createRequire(import.meta.url)
const nextConfig = require('../next.config.js')

const CANONICAL = '/api/v1'
const ALIAS = '/api/terrapod/v1'

/** The SSE endpoints, by suffix. Kept here so the test fails if one is dropped. */
const SSE_SUFFIXES = [
  '/listeners/:path*',
  '/workspaces/:path*/runs/events',
  '/workspace-events',
  '/agent-pools/:path*/events',
]

test('every SSE path carries Content-Encoding: none on both prefixes', async () => {
  const rules = await nextConfig.headers()
  const sources = new Map<string, string[]>()
  for (const rule of rules) {
    sources.set(
      rule.source,
      (rule.headers ?? []).map((h: { key: string; value: string }) => `${h.key}: ${h.value}`),
    )
  }

  for (const prefix of [CANONICAL, ALIAS]) {
    for (const suffix of SSE_SUFFIXES) {
      const source = `${prefix}${suffix}`
      assert.ok(
        sources.has(source),
        `${source} has no headers rule — SSE on it will be buffered and deliver nothing`,
      )
      assert.ok(
        sources.get(source)!.includes('Content-Encoding: none'),
        `${source} is missing the SSE passthrough header`,
      )
    }
  }
})

test('the alias is not accidentally dropped when the canonical prefix is present', () => {
  // The specific regression: someone "tidies up" by deleting the legacy entries
  // once the frontend moves to /api/v1 — while our own runner and listener
  // images, and any un-upgraded integration, are still on the alias.
  const config = readFileSync(new URL('../next.config.js', import.meta.url), 'utf8')
  assert.ok(
    config.includes(ALIAS),
    'next.config.js no longer references the deprecated alias; SSE through it would ' +
      'silently stop delivering events while still returning 200',
  )
})
