/**
 * Large config-version upload through the BFF (#884).
 *
 * Regression guard for the config-version upload path. Uploads (config-version
 * tarballs, state blobs, provider binaries) flow client → BFF → API, and the
 * API streams them straight to object storage. The bug: the BFF proxied /api/*
 * via Next.js *middleware*, which buffers the request body and caps it at
 * `middlewareClientMaxBodySize` (10 MB). Any upload over that was truncated and
 * the proxied PUT died with a `socket hang up` → 500 → no run ever created, so
 * CLI-driven runs were impossible for any real repo (a monorepo config slug is
 * tens of MB once modules are bundled).
 *
 * The fix moves the /api/* proxy to a streaming Route Handler
 * (web/src/app/api/[...path]/route.ts) that forwards `request.body` unbuffered.
 * This spec proves it end-to-end through the REAL BFF proxy chain: a body
 * comfortably over the old 10 MB cap must upload with a 200 and the CV must
 * actually reach `uploaded` (bytes landed, not truncated). It would fail on the
 * pre-fix middleware and passes on the route handler.
 *
 * The upload PUT MUST target BASE_URL (the BFF web tier), not API_URL — routing
 * it straight at the API would bypass exactly the layer under test.
 */
import { test, expect } from '@playwright/test'
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api'

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000'
const API_URL = process.env.API_URL || 'http://localhost:8000'

test.describe('Large upload through BFF', () => {
  test('a >10MB config tarball uploads through the BFF (not capped/buffered)', async () => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2e-bigupload'))

    // Create a configuration version (small request — API-direct setup is fine).
    const cvRes = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/configuration-versions`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/vnd.api+json',
      },
      body: JSON.stringify({
        data: {
          type: 'configuration-versions',
          attributes: { 'auto-queue-runs': false, speculative: true },
        },
      }),
    })
    expect(cvRes.status).toBe(201)
    const cvId = (await cvRes.json()).data.id as string

    // 12 MB — deliberately over the old 10 MB middleware body cap. The upload
    // endpoint doesn't parse the tarball (that happens at run time), so opaque
    // bytes are a valid body: it only needs to be non-empty and land whole.
    const body = Buffer.alloc(12 * 1024 * 1024, 7)

    // THROUGH THE BFF (BASE_URL) — the layer under test.
    const upRes = await fetch(`${BASE_URL}/api/v2/configuration-versions/${cvId}/upload`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/octet-stream' },
      body,
    })
    expect(upRes.status).toBe(200)

    // The CV actually reached `uploaded` — proves the full body landed in
    // storage, not a truncated first 10 MB.
    const showRes = await fetch(`${API_URL}/api/v2/configuration-versions/${cvId}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(showRes.status).toBe(200)
    expect((await showRes.json()).data.attributes.status).toBe('uploaded')
  })
})
