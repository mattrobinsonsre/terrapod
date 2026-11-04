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

// The CSP directives beside frame-ancestors. Each forbids something the console
// does not do, so none can break it — and img-src is the floor under the
// model-authored-markdown fix: a surface that forgets `img: () => null` still
// cannot fetch a remote pixel on the model's behalf.
test('the CSP carries the no-compatibility-cost directives', async () => {
  const entries = await nextConfig.headers()
  const csp = String(
    entries
      .filter((e: { source: string }) => e.source === '/:path*')
      .flatMap((e: { headers: { key: string; value: string }[] }) => e.headers)
      .find((h: { key: string }) => h.key === 'Content-Security-Policy')?.value,
  )

  const directives = new Map(
    csp.split(';').map((d: string) => {
      const [name, ...rest] = d.trim().split(/\s+/)
      return [name, rest.join(' ')]
    }),
  )

  assert.equal(directives.get('frame-ancestors'), "'none'")
  assert.equal(directives.get('img-src'), "'self' data: blob:")
  assert.equal(directives.get('object-src'), "'none'")
  assert.equal(directives.get('base-uri'), "'self'")
  assert.equal(directives.get('form-action'), "'self'")
})

// The regression this file did not catch, and the reason it could not.
//
// /api-docs frames the API's own ReDoc and Swagger UI. `frame-src 'self'`
// above grants the PARENT the right to embed; it says nothing about whether
// the FRAMED document consents, and the catch-all's `frame-ancestors 'none'`
// plus `X-Frame-Options: DENY` refused that consent for every path -- those
// two included, since they are served through this same BFF. The page was
// blank from #1767 (2026-09-22) until 2026-11-02.
//
// Asserting the catch-all in isolation is what let it through: every
// assertion above was true, and the page was broken. So this asserts the
// RELATIONSHIP -- that the documents the console frames are allowed to be
// framed by it, while everything else is not.
test('the framed documentation endpoints consent to same-origin framing', async () => {
  const entries = await nextConfig.headers()
  const framed = ['/api/docs', '/api/redoc']

  for (const source of framed) {
    const rule = entries.find((e: { source: string }) => e.source === source)
    assert.ok(rule, `${source} has no header rule, so the catch-all's DENY applies and the page is blank`)
    const byKey = new Map(
      rule.headers.map((h: { key: string; value: string }) => [h.key, h.value]),
    )
    assert.equal(byKey.get('X-Frame-Options'), 'SAMEORIGIN', `${source} must consent to same-origin framing`)
    const csp = String(byKey.get('Content-Security-Policy'))
    assert.match(csp, /frame-ancestors 'self'/, `${source} must allow a same-origin ancestor`)
    assert.doesNotMatch(csp, /frame-ancestors 'none'/)
    // Everything the catch-all forbids and the console does not do stays
    // forbidden here too -- this is an exception about framing, nothing else.
    for (const d of ["object-src 'none'", "base-uri 'self'", "form-action 'self'"]) {
      assert.ok(csp.includes(d), `${source} dropped ${d}`)
    }
  }

  // And the exception is exactly those two: anything else framed by the
  // console would be a new decision, not an oversight.
  const sameOrigin = entries
    .filter((e: { headers: { key: string; value: string }[] }) =>
      e.headers.some((h) => h.key === 'X-Frame-Options' && h.value === 'SAMEORIGIN'),
    )
    .map((e: { source: string }) => e.source)
    .sort()
  assert.deepEqual(sameOrigin, framed.slice().sort())
})

// frame-src is 'self', NOT 'none': /api-docs frames the API's own ReDoc and
// Swagger UI at /api/redoc and /api/docs, both same-origin through the BFF.
// 'none' would leave that page showing an empty frame, with nothing failing in
// CI to say so.
test('frame-src permits the same-origin API docs frames', async () => {
  const entries = await nextConfig.headers()
  const csp = String(
    entries
      .filter((e: { source: string }) => e.source === '/:path*')
      .flatMap((e: { headers: { key: string; value: string }[] }) => e.headers)
      .find((h: { key: string }) => h.key === 'Content-Security-Policy')?.value,
  )

  assert.match(csp, /frame-src 'self'/)
  assert.doesNotMatch(csp, /frame-src 'none'/)
})

// If script-src or style-src is ever added it must be nonce- or hash-based.
// 'unsafe-inline' would read as a tightening in a diff while permitting exactly
// what the directive exists to stop — the App Router's inline bootstrap is why
// neither is set today.
test('no CSP directive is relaxed with unsafe-inline or unsafe-eval', async () => {
  const entries = await nextConfig.headers()
  const csp = String(
    entries
      .filter((e: { source: string }) => e.source === '/:path*')
      .flatMap((e: { headers: { key: string; value: string }[] }) => e.headers)
      .find((h: { key: string }) => h.key === 'Content-Security-Policy')?.value,
  )

  assert.doesNotMatch(csp, /'unsafe-inline'/)
  assert.doesNotMatch(csp, /'unsafe-eval'/)
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
