import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  EXPECTED_REWRITES,
  PLACEHOLDER,
  apiBase,
  applyPatch,
  patchManifest,
} from '../server-entry.mjs'

const manifest = (dest: string) =>
  JSON.stringify({
    version: 3,
    rewrites: {
      beforeFiles: [
        { source: '/api/v1/storage/get/:path*', destination: `${dest}/api/v1/storage/get/:path*` },
        {
          source: '/api/terrapod/v1/storage/get/:path*',
          destination: `${dest}/api/terrapod/v1/storage/get/:path*`,
        },
      ],
    },
  })

function withManifest(text: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'tp-manifest-'))
  const path = join(dir, 'routes-manifest.json')
  writeFileSync(path, text)
  return path
}

describe('apiBase', () => {
  it('matches the Route Handler’s own fallback, so the two cannot disagree', () => {
    assert.equal(apiBase({}), 'http://localhost:8001')
  })

  it('uses API_URL and strips trailing slashes', () => {
    assert.equal(apiBase({ API_URL: 'http://terrapod-api:8000' }), 'http://terrapod-api:8000')
    assert.equal(apiBase({ API_URL: 'http://terrapod-api:8000/' }), 'http://terrapod-api:8000')
    assert.equal(apiBase({ API_URL: 'http://terrapod-api:8000///' }), 'http://terrapod-api:8000')
  })
})

describe('patchManifest', () => {
  it('replaces every occurrence and reports how many', () => {
    const { text, replaced } = patchManifest(manifest(PLACEHOLDER), 'http://api:8000')
    assert.equal(replaced, EXPECTED_REWRITES)
    assert.ok(!text.includes(PLACEHOLDER))
    assert.equal(text.split('http://api:8000').length - 1, EXPECTED_REWRITES)
  })

  it('reports zero rather than throwing when there is nothing to replace', () => {
    // The decision to refuse belongs to applyPatch, which is the thing with a
    // container to stop. Keeping this pure makes the refusal testable.
    const { replaced } = patchManifest(manifest('http://already:8000'), 'http://api:8000')
    assert.equal(replaced, 0)
  })
})

describe('applyPatch refuses rather than serving chunked responses (#2639)', () => {
  it('patches a well-formed manifest and reads it back', () => {
    const path = withManifest(manifest(PLACEHOLDER))
    const replaced = applyPatch(path, 'http://terrapod-api:8000')
    assert.equal(replaced, EXPECTED_REWRITES)
    const after = readFileSync(path, 'utf8')
    assert.ok(!after.includes(PLACEHOLDER))
    assert.ok(after.includes('http://terrapod-api:8000/api/v1/storage/get/:path*'))
  })

  it('throws when the manifest is missing — Next renamed or moved it', () => {
    // The loud half of the design: a format change must stop the container, not
    // leave Pulumi and docker quietly back on chunked encoding.
    assert.throws(
      () => applyPatch(join(tmpdir(), 'tp-no-such-manifest', 'routes-manifest.json'), 'http://api:8000'),
      /cannot read [\s\S]*routes-manifest\.json[\s\S]*Refusing to start/,
    )
  })

  it('throws when the placeholder count is not what next.config.js declares', () => {
    const oneOnly = JSON.stringify({
      rewrites: { beforeFiles: [{ source: '/a', destination: `${PLACEHOLDER}/a` }] },
    })
    assert.throws(
      () => applyPatch(withManifest(oneOnly), 'http://api:8000'),
      /expected 2 placeholder destination\(s\)[\s\S]*found 1[\s\S]*Refusing to start/,
    )
  })

  it('throws when the manifest carries no placeholder at all', () => {
    // This is the #47 shape: a baked destination that silently survives. It
    // must not be mistaken for "nothing to do".
    assert.throws(
      () => applyPatch(withManifest(manifest('http://localhost:8001')), 'http://api:8000'),
      /found 0[\s\S]*Refusing to start/,
    )
  })
})

describe('the config and the entrypoint must not drift', () => {
  const config = readFileSync(new URL('../next.config.js', import.meta.url), 'utf8')

  it('next.config.js uses the same placeholder string', () => {
    // Two files holding one literal is how the assertion comes to fail
    // spuriously, or worse, to pass while protecting nothing.
    assert.ok(
      config.includes(`'${PLACEHOLDER}'`),
      `next.config.js does not declare ${PLACEHOLDER}; server-entry.mjs would find nothing to patch`,
    )
  })

  it('EXPECTED_REWRITES equals the number of placeholder destinations declared', () => {
    // Derived from the config rather than written down twice, so adding a third
    // length-requiring path updates the assertion by construction.
    const declared = config.split('${STORAGE_REWRITE_PLACEHOLDER}').length - 1
    assert.equal(
      declared,
      EXPECTED_REWRITES,
      `next.config.js declares ${declared} placeholder destination(s) but ` +
        `server-entry.mjs expects ${EXPECTED_REWRITES}`,
    )
  })

  it('the placeholder cannot resolve, so a failed patch cannot look like success', () => {
    // RFC 2606 reserves .invalid. #47 was an operator whose web pod proxied to a
    // baked localhost:8001 — a host that DOES resolve, which is what made the
    // misconfiguration look like a working deployment.
    assert.match(PLACEHOLDER, /\.invalid$/)
    assert.ok(!PLACEHOLDER.includes('localhost'))
  })
})
