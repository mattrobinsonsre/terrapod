import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { test } from 'node:test'

// next.config.js is CommonJS; these tests run as ES modules.
const nextConfig = createRequire(import.meta.url)('../next.config.js')

// GHSA-46gw-rvrr-jqfx. The API sets these on every response; page routes got
// only HSTS, so the console's own pages could be framed while the API they call
// could not — and the pages are where a human clicks queue-apply,
// delete-workspace and force-unlock.
test('page routes carry the clickjacking and sniffing headers', async () => {
  const entries = await nextConfig.headers()
  const pageHeaders = entries
    .filter((e: { source: string }) => e.source === '/:path*')
    .flatMap((e: { headers: { key: string; value: string }[] }) => e.headers)

  const byKey = new Map(pageHeaders.map((h: { key: string; value: string }) => [h.key, h.value]))

  assert.equal(byKey.get('X-Frame-Options'), 'DENY')
  assert.equal(byKey.get('X-Content-Type-Options'), 'nosniff')
  assert.equal(byKey.get('Referrer-Policy'), 'strict-origin-when-cross-origin')
  assert.match(String(byKey.get('Content-Security-Policy')), /frame-ancestors 'none'/)
})

// The headers array carries the ONLY thing keeping SSE log streaming
// unbuffered. Redefining it rather than appending would take those out
// silently: nothing errors, the log simply stops updating. This is the
// regression that a "fix the headers" change is most likely to cause.
test('the SSE Content-Encoding entries survive', async () => {
  const entries = await nextConfig.headers()
  const sse = entries.filter((e: { headers: { key: string }[] }) =>
    e.headers.some((h: { key: string }) => h.key === 'Content-Encoding'),
  )

  assert.ok(sse.length >= 8, `expected the SSE entries to remain, found ${sse.length}`)
  for (const entry of sse) {
    assert.equal(entry.headers[0].value, 'none')
  }
})
