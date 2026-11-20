import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, statSync, utimesSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  EXPECTED_REWRITES,
  PLACEHOLDER,
  apiBase,
  applyPatch,
  assertPatched,
  ensurePatched,
  occurrences,
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

describe('--emit writes a copy, so a read-only root can still be served (#2639)', () => {
  it('patches into `out` and leaves the source alone', () => {
    const path = withManifest(manifest(PLACEHOLDER))
    const out = join(mkdtempSync(join(tmpdir(), 'tp-emit-')), 'routes-manifest.json')

    assert.equal(applyPatch(path, 'http://api:8000', { out }), EXPECTED_REWRITES)

    // The source is what the IMAGE ships, and an init container must not need
    // to write to it -- that is the whole point of emitting elsewhere.
    assert.equal(occurrences(readFileSync(path, 'utf8'), PLACEHOLDER), EXPECTED_REWRITES)
    assert.equal(occurrences(readFileSync(out, 'utf8'), PLACEHOLDER), 0)
    assert.equal(occurrences(readFileSync(out, 'utf8'), 'http://api:8000'), EXPECTED_REWRITES)
  })

  it('names the init container when the write is refused, because that is the fix', () => {
    const dir = mkdtempSync(join(tmpdir(), 'tp-emit-'))
    const path = join(dir, 'routes-manifest.json')
    writeFileSync(path, manifest(PLACEHOLDER))

    // ENOTDIR rather than a mode: the suite runs as root in the test image,
    // where CAP_DAC_OVERRIDE walks straight through a permission bit, so a
    // chmod-based fixture asserts a property of the uid instead of the code.
    const out = join(path, 'routes-manifest.json')
    assert.throws(() => applyPatch(path, 'http://api:8000', { out }), /init container/)
  })
})

describe('ensurePatched accepts an already-patched manifest WITHOUT writing', () => {
  const base = 'http://api:8000'

  it('does not touch a manifest the init container already pointed at the API', () => {
    const path = withManifest(manifest(base))
    // A write updates mtime even when the bytes are identical, so pinning it
    // is how "did not write" becomes observable rather than asserted.
    const past = new Date(Date.now() - 60_000)
    utimesSync(path, past, past)
    const before = statSync(path).mtimeMs

    assert.equal(ensurePatched(path, base), 0)
    assert.equal(statSync(path).mtimeMs, before)
  })

  it('still patches in place when the placeholder is there, for a plain docker run', () => {
    const path = withManifest(manifest(PLACEHOLDER))
    assert.equal(ensurePatched(path, base), EXPECTED_REWRITES)
    assert.equal(occurrences(readFileSync(path, 'utf8'), base), EXPECTED_REWRITES)
  })

  it('refuses a manifest with the placeholder gone and the API absent', () => {
    // The dangerous shape: nothing left to patch, so a naive "no placeholders
    // means we are done" check passes while every storage read proxies to a
    // host this pod was never told about. That is #47.
    const path = withManifest(manifest('http://somewhere-else:8000'))
    assert.throws(() => ensurePatched(path, base), /names http:\/\/api:8000 0 time/)
  })

  it('refuses a manifest naming the API fewer times than there are rewrites', () => {
    // A count, not a substring: one destination pointed at the API and one left
    // somewhere else would satisfy `includes(base)` and serve half the clients
    // chunked responses.
    const half = manifest(PLACEHOLDER)
      .split(`${PLACEHOLDER}/api/v1/storage/get/:path*`)
      .join(`${base}/api/v1/storage/get/:path*`)
      .split(`${PLACEHOLDER}/api/terrapod/v1/storage/get/:path*`)
      .join('http://elsewhere:8000/api/terrapod/v1/storage/get/:path*')
    const path = withManifest(half)
    assert.equal(occurrences(readFileSync(path, 'utf8'), PLACEHOLDER), 0)
    assert.throws(() => assertPatched(path, base), /expected at least 2/)
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
