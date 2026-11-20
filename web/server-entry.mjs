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
  const replaced = text.split(PLACEHOLDER).length - 1
  return { text: text.split(PLACEHOLDER).join(base), replaced }
}

/**
 * Patch the manifest on disk and refuse to continue unless it took.
 *
 * Throws rather than returning a status: the caller is a container entrypoint,
 * and a half-applied rewrite is the #47 failure with extra steps.
 */
export function applyPatch(manifestPath, base, { expected = EXPECTED_REWRITES } = {}) {
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
  writeFileSync(manifestPath, text)

  // Read back, because a successful write is not evidence the file now says
  // what we think. An absent signal reading as a passing one is how this class
  // of bug survives.
  const after = readFileSync(manifestPath, 'utf8')
  if (after.includes(PLACEHOLDER)) {
    throw new Error(
      `storage rewrite: ${manifestPath} still contains the placeholder after ` +
        'patching. Refusing to start: see #2639.',
    )
  }
  if (!after.includes(base)) {
    throw new Error(
      `storage rewrite: ${manifestPath} does not contain ${base} after patching. ` +
        'Refusing to start: see #2639.',
    )
  }
  return replaced
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]
if (isMain) {
  const here = dirname(fileURLToPath(import.meta.url))
  const base = apiBase()
  const replaced = applyPatch(join(here, '.next', 'routes-manifest.json'), base)
  console.log(`[storage-rewrite] pointed ${replaced} rewrite(s) at ${base}`)
  await import('./server.js')
}
