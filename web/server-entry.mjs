// Container entrypoint: patch the storage rewrite's destination from the
// runtime `API_URL`, assert it took, then hand over to Next's own server.
//
// WHY THIS FILE EXISTS, because a wrapper around `server.js` looks gratuitous.
//
// Next >= 16.4.5 strips `content-length` from every App Router route handler
// response (`FORBIDDEN_HEADERS` in `send-response.js`) and always pipes the
// body, so Node falls back to chunked encoding. Measured: a route handler
// cannot emit the header at all, and buffering does not help, because
// `new Response(arrayBuffer).body` is a ReadableStream like any other.
//
// Clients that REQUIRE the header then break. Pulumi refuses a plugin download
// outright -- `expected -1 bytes but copied 18043696` -- and docker's registry
// client needs one on a blob HEAD. Those bodies reach the BFF through the
// filesystem backend's presigned `storage/get` URL.
//
// An external `rewrites()` destination is proxied by Next itself rather than by
// a route handler, and measured end to end in the standalone output we actually
// ship: the header survives on GET and HEAD, and the rewrite wins over the
// `/api/[...path]` catch-all. 16.4.5 also fixes CVE-2026-119348, a
// response-framing desync in exactly our proxy pattern, so the bump is wanted
// on its own.
//
// The catch is that a rewrite destination is BAKED into
// `.next/routes-manifest.json` at build time. `next.config.js` is re-evaluated
// when the server starts, but rewrites are served from the manifest, so a
// `process.env.API_URL` in the config is ignored -- which is #47, an
// outside-reported outage where the web pod proxied to the baked
// `http://localhost:8001` while the pod's own `API_URL` said otherwise.
//
// So the destination is baked as an unresolvable placeholder and rewritten
// here, and the assertion below is NOT optional: #47 is what happens when a
// baked destination silently survives. A Next release that changes the manifest
// format must fail loudly at boot rather than leave clients quietly back on
// chunked encoding.
//
// TWO WAYS IN, because the chart runs this container with
// `readOnlyRootFilesystem: true` and patching in place is a write.
//
//   * `--emit <path>` patches a COPY and starts nothing. The chart runs that in
//     an init container, writing into an emptyDir the main container then mounts
//     over `.next/routes-manifest.json` via `subPath`. The security posture is
//     untouched and the assertion still gates startup, because a failed init
//     container means the pod never serves.
//   * No argument patches in place, which is what a plain `docker run` of this
//     image does on a writable filesystem.
//
// On startup an already-patched manifest is accepted WITHOUT a write -- that is
// the init-container case, and writing would be the EROFS the arrangement
// exists to avoid. It is accepted only after the same assertions a fresh patch
// has to pass, so a stale or half-patched mount is still refused.

import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

/** Unresolvable by construction (RFC 2606 reserves `.invalid`), unlike the
 *  `localhost:8001` default that made #47 look like a working deployment. */
export const PLACEHOLDER = 'http://terrapod-storage-rewrite.invalid'

/** How many rewrite destinations carry the placeholder. Both API prefixes are
 *  served (`/api/v1` is canonical, `/api/terrapod/v1` the deprecated alias
 *  still used by the presigned URL builder), so both are rewritten. */
export const EXPECTED_REWRITES = 2

/**
 * Resolve the API base the same way the Route Handler does, so the two cannot
 * disagree about where the API is.
 *
 * `env` is typed as a plain record rather than `ProcessEnv` so a test can pass
 * exactly the variables it means to, without inventing a `NODE_ENV`.
 *
 * @param {Record<string, string | undefined>} [env]
 * @returns {string}
 */
export function apiBase(env = process.env) {
  return (env.API_URL || 'http://localhost:8001').replace(/\/+$/, '')
}

/**
 * Replace the placeholder with `base` in a routes manifest.
 *
 * Returns `{ text, replaced }`. Pure, so the assertion below can be tested
 * without a built manifest.
 */
export function patchManifest(text, base) {
  const replaced = occurrences(text, PLACEHOLDER)
  return { text: text.split(PLACEHOLDER).join(base), replaced }
}

/**
 * How many times `needle` occurs in `haystack`.
 *
 * Both the patch count and the read-back check go through this, so "the
 * placeholder is gone" and "the base arrived" are the same kind of claim --
 * a count -- rather than one count and one `includes()` that is satisfied by a
 * single occurrence however many were expected.
 *
 * @param {string} haystack
 * @param {string} needle
 * @returns {number}
 */
export function occurrences(haystack, needle) {
  return haystack.split(needle).length - 1
}

/**
 * Patch the manifest on disk and refuse to continue unless it took.
 *
 * Throws rather than returning a status: the caller is a container entrypoint,
 * and a half-applied rewrite is the #47 failure with extra steps.
 *
 * `out` writes the patched manifest somewhere else and leaves the source alone,
 * which is how the chart's init container works: the image's own manifest is on
 * a read-only filesystem by then.
 *
 * @param {string} manifestPath
 * @param {string} base
 * @param {{ expected?: number, out?: string }} [opts]
 * @returns {number}
 */
export function applyPatch(manifestPath, base, { expected = EXPECTED_REWRITES, out } = {}) {
  const target = out ?? manifestPath
  let before
  try {
    before = readFileSync(manifestPath, 'utf8')
  } catch (err) {
    throw new Error(
      `storage rewrite: cannot read ${manifestPath} (${err.code ?? err.message}). ` +
        'Next moved or renamed the routes manifest, so the rewrite that gives ' +
        'Pulumi and docker a Content-Length cannot be pointed at the API. ' +
        'Refusing to start: see #2639.',
    )
  }
  const { text, replaced } = patchManifest(before, base)
  if (replaced !== expected) {
    throw new Error(
      `storage rewrite: expected ${expected} placeholder destination(s) in ` +
        `${manifestPath}, found ${replaced}. Either next.config.js and this file ` +
        'disagree about how many rewrites carry the placeholder, or Next changed ' +
        'the manifest format. Refusing to start rather than serving chunked ' +
        'responses to clients that require a Content-Length: see #2639.',
    )
  }
  try {
    writeFileSync(target, text)
  } catch (err) {
    // The one failure an operator can act on, so it says what to do. Reached
    // when the root filesystem is read-only and nothing seeded the manifest --
    // i.e. the chart's init container did not run.
    throw new Error(
      `storage rewrite: cannot write ${target} (${err.code ?? err.message}). ` +
        'On a read-only root filesystem the manifest must be patched by the ' +
        "chart's web init container, which runs this file with `--emit` and " +
        'mounts the result over the manifest. Refusing to start: see #2639.',
    )
  }
  assertPatched(target, base, expected)
  return replaced
}

/**
 * Refuse unless `path` is a manifest that names `base` and no longer names the
 * placeholder.
 *
 * Separate from `applyPatch` because it is the gate on BOTH routes in: a fresh
 * patch reads its own write back, and an already-patched manifest mounted by
 * the init container has to clear the same bar before it is trusted. A write is
 * not evidence the file says what we think, and neither is a mount.
 *
 * @param {string} path
 * @param {string} base
 * @param {number} expected
 */
export function assertPatched(path, base, expected = EXPECTED_REWRITES) {
  const after = readFileSync(path, 'utf8')
  const left = occurrences(after, PLACEHOLDER)
  if (left > 0) {
    throw new Error(
      `storage rewrite: ${path} still contains ${left} placeholder ` +
        'destination(s). Refusing to start: see #2639.',
    )
  }
  const pointed = occurrences(after, base)
  if (pointed < expected) {
    throw new Error(
      `storage rewrite: ${path} names ${base} ${pointed} time(s), expected at ` +
        `least ${expected}. A manifest with the placeholder gone and the API ` +
        'absent proxies storage reads to nothing, which is #47. Refusing to ' +
        'start: see #2639.',
    )
  }
}

/**
 * Point the manifest at `base`, whether or not something already did.
 *
 * Returns how many destinations this call rewrote -- `0` meaning the manifest
 * arrived patched, which is the init-container case and must NOT provoke a
 * write, because the filesystem it would write to is read-only by design.
 *
 * @param {string} manifestPath
 * @param {string} base
 * @param {{ expected?: number }} [opts]
 * @returns {number}
 */
export function ensurePatched(manifestPath, base, { expected = EXPECTED_REWRITES } = {}) {
  const before = readFileSync(manifestPath, 'utf8')
  if (occurrences(before, PLACEHOLDER) === 0) {
    assertPatched(manifestPath, base, expected)
    return 0
  }
  return applyPatch(manifestPath, base, { expected })
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]
if (isMain) {
  const here = dirname(fileURLToPath(import.meta.url))
  const manifest = join(here, '.next', 'routes-manifest.json')
  const base = apiBase()

  const emitAt = process.argv.indexOf('--emit')
  if (emitAt !== -1) {
    const out = process.argv[emitAt + 1]
    if (!out) {
      throw new Error('storage rewrite: --emit needs a path to write to.')
    }
    const replaced = applyPatch(manifest, base, { out })
    console.log(`[storage-rewrite] wrote ${out}, ${replaced} rewrite(s) at ${base}`)
  } else {
    const replaced = ensurePatched(manifest, base)
    const how = replaced === 0 ? 'already pointed' : `pointed ${replaced} rewrite(s)`
    console.log(`[storage-rewrite] ${how} at ${base}`)
    await import('./server.js')
  }
}
