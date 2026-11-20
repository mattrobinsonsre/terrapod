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
  hasOrigin,
  hasPlaceholderOrigin,
  occurrences,
  patchManifest,
  rewriteRules,
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
    assert.throws(() => ensurePatched(path, base), /0 rewrite destination\(s\) on http:\/\/api:8000/)
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
    assert.throws(() => assertPatched(path, base), /1 rewrite destination\(s\) on .*expected at least 2/)
  })
})

describe('the destination is matched by ORIGIN, not by substring', () => {
  it('refuses a host that merely contains the placeholder', () => {
    // `includes()` says yes to this, and a bare `startsWith` says yes too --
    // the host is somebody else's and the rewrite would be pointed at them.
    const impostor = `${PLACEHOLDER}.example.test`
    assert.equal(hasPlaceholderOrigin(impostor), false)
    assert.equal(hasPlaceholderOrigin(`${impostor}/api/v1/storage/get/x`), false)
  })

  it('accepts the placeholder itself and any path under it', () => {
    assert.equal(hasPlaceholderOrigin(PLACEHOLDER), true)
    assert.equal(hasPlaceholderOrigin(`${PLACEHOLDER}/api/v1/storage/get/:path*`), true)
  })

  it('applies the same boundary to the base, so a lookalike cannot satisfy the count', () => {
    assert.equal(hasOrigin('http://api:8000/x', 'http://api:8000'), true)
    assert.equal(hasOrigin('http://api:8000.evil.test/x', 'http://api:8000'), false)
  })

  it('leaves a destination whose origin is a lookalike untouched', () => {
    const doc = {
      rewrites: {
        beforeFiles: [
          { source: '/a', destination: `${PLACEHOLDER}/a` },
          { source: '/b', destination: `${PLACEHOLDER}.example.test/b` },
        ],
      },
    }
    const { text, replaced } = patchManifest(JSON.stringify(doc), 'http://api:8000')
    assert.equal(replaced, 1)
    const rules = rewriteRules(JSON.parse(text))
    assert.equal(rules[0].destination, 'http://api:8000/a')
    assert.equal(rules[1].destination, `${PLACEHOLDER}.example.test/b`)
  })

  it('reads rewrites whether Next writes a group map or a flat array', () => {
    const rule = { source: '/a', destination: `${PLACEHOLDER}/a` }
    assert.equal(rewriteRules({ rewrites: { beforeFiles: [rule] } }).length, 1)
    assert.equal(rewriteRules({ rewrites: [rule] }).length, 1)
    // A shape it cannot read must come back empty rather than throwing, so the
    // count assertion is what refuses -- a silent zero is the failure mode.
    assert.equal(rewriteRules({ rewrites: { beforeFiles: 'nonsense' } }).length, 0)
    assert.equal(rewriteRules({}).length, 0)
  })
})

describe('the two placeholder checks are not interchangeable, so each is pinned by its message', () => {
  const base = 'http://api:8000'

  // A manifest whose DESTINATIONS are all pointed at the API but which still
  // mentions the placeholder somewhere else. Both checks fire on the shape
  // below; only the structural one can tell you which case you are in, and a
  // mutation that deletes it is invisible unless the message is asserted.
  const strayMention = () =>
    JSON.stringify({
      version: 3,
      rewrites: {
        beforeFiles: [
          { source: '/api/v1/storage/get/:path*', destination: `${base}/api/v1/storage/get/:path*` },
          {
            source: '/api/terrapod/v1/storage/get/:path*',
            destination: `${base}/api/terrapod/v1/storage/get/:path*`,
          },
        ],
      },
      // Not a destination: something else in the document carries it.
      headers: [{ source: '/x', headers: [{ key: 'x-upstream', value: PLACEHOLDER }] }],
    })

  it('a placeholder IN a destination is reported as a destination problem', () => {
    const path = withManifest(manifest(PLACEHOLDER))
    assert.throws(
      () => assertPatched(path, base),
      /rewrite destination\(s\) on the placeholder origin/,
    )
  })

  it('a placeholder OUTSIDE every destination is reported as exactly that', () => {
    const path = withManifest(strayMention())
    assert.throws(() => assertPatched(path, base), /outside a rewrite destination/)
  })

  it('ensurePatched decides what to do from the destinations, not the whole document', () => {
    // Every destination is already pointed at the API, so there is nothing to
    // patch and nothing to WRITE -- which matters, because the filesystem is
    // read-only in the deployment this runs in. Counting the stray mention as
    // work to do would attempt a write and fail with EROFS instead of
    // reporting the real problem.
    const path = withManifest(strayMention())
    assert.throws(() => ensurePatched(path, base), /outside a rewrite destination/)
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
    //
    // Counts `${storageRewriteBase()}` rather than the placeholder literal: the
    // destinations resolve the base when the config is evaluated, so that call
    // is what marks a destination as one of ours. The placeholder now appears
    // in the config twice for reasons that are not destinations -- its own
    // declaration and the fallback inside that helper -- so counting it would
    // give 2 by coincidence and keep giving 2 if a destination were deleted.
    const declared = config.split('${storageRewriteBase()}').length - 1
    assert.equal(
      declared,
      EXPECTED_REWRITES,
      `next.config.js declares ${declared} placeholder destination(s) but ` +
        `server-entry.mjs expects ${EXPECTED_REWRITES}`,
    )
  })

  it('the base falls back to the placeholder, never to a host that resolves', () => {
    // `API_URL` is unset during `npm run build` (Dockerfile.web declares no such
    // ARG or ENV), so this fallback is what a production build bakes -- and the
    // init container patches it at pod start. A fallback to anything that
    // RESOLVES is #47: the pod proxies somewhere wrong and looks healthy.
    assert.match(
      config,
      /process\.env\.API_URL \|\| STORAGE_REWRITE_PLACEHOLDER/,
      'next.config.js must fall back to the placeholder when API_URL is unset',
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
