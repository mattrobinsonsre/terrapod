// Which upstream failures the BFF retries (#1381).
//
// The interesting decision in the proxy is not the forwarding, it is whether a
// given failure earns a second attempt. Get it wrong in one direction and a
// transient reset fails a run; wrong in the other and we replay a request the
// API may already have acted on. So the predicate is tested directly, including
// the shape that actually occurred in production — undici nesting the real
// cause several levels below the error you catch.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isRetryableTransportError,
  isRetryableRequest,
  forwardedContentLength,
} from '../src/lib/bff-proxy.ts'

function errno(message: string, code?: string): Error {
  const e: NodeJS.ErrnoException = new Error(message)
  if (code) e.code = code
  return e
}

test('a bare ECONNRESET is retryable', () => {
  assert.equal(isRetryableTransportError(errno('read ECONNRESET', 'ECONNRESET')), true)
})

test('an ECONNRESET nested in a cause chain is still found', () => {
  // What the failure in #1381 actually looked like: fetch rejects with a
  // generic error and the code is two levels down. A predicate that only
  // inspected the top-level error would have retried nothing.
  const inner = errno('read ECONNRESET', 'ECONNRESET')
  const mid = new Error('other side closed', { cause: inner })
  const outer = new TypeError('fetch failed', { cause: mid })
  assert.equal(isRetryableTransportError(outer), true)
})

test('"socket hang up" is retryable even with no error code attached', () => {
  // The exact message logged in #1381, which arrived without a code.
  assert.equal(isRetryableTransportError(errno('socket hang up')), true)
})

test('a connection refused is retryable', () => {
  assert.equal(isRetryableTransportError(errno('connect ECONNREFUSED', 'ECONNREFUSED')), true)
})

test('an unrelated error is not retryable', () => {
  // A bug in our own proxy code must surface, not be silently attempted twice.
  assert.equal(isRetryableTransportError(new TypeError('headers.set is not a function')), false)
})

test('a cause chain of unrelated errors is not retryable', () => {
  const outer = new Error('a', { cause: new Error('b', { cause: new Error('c') }) })
  assert.equal(isRetryableTransportError(outer), false)
})

test('a cyclic cause chain terminates', () => {
  // Defensive: a self-referencing cause must not hang the request.
  const a = new Error('a')
  ;(a as { cause?: unknown }).cause = a
  assert.equal(isRetryableTransportError(a), false)
})

test('a non-Error rejection is not retryable', () => {
  assert.equal(isRetryableTransportError('socket hang up'), false)
  assert.equal(isRetryableTransportError(undefined), false)
})

test('GET and HEAD are retryable; state-changing methods are not', () => {
  // The safety argument for retrying rests on the method being idempotent by
  // definition, so we never have to prove the upstream did not act on it.
  assert.equal(isRetryableRequest('GET'), true)
  assert.equal(isRetryableRequest('HEAD'), true)
  assert.equal(isRetryableRequest('OPTIONS'), true)
  for (const m of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    assert.equal(isRetryableRequest(m), false, `${m} must not be retried`)
  }
})

test('a request carrying a body is never retried', () => {
  // The body is a stream consumed by the first attempt — there is nothing left
  // to send on a second one, so a "retry" would forward a bodiless request.
  assert.equal(isRetryableRequest('GET', true), false)
})

// Which Content-Length reaches the client (#1408).
//
// The proxy strips response framing headers and lets undici re-derive them,
// which is right for a body it may have decompressed and wrong for one it has
// not. Getting this wrong is not subtle in effect: without the header, docker
// refuses every blob HEAD and no image can be pulled through the BFF at all.

test('an uncompressed response keeps its Content-Length', () => {
  const headers = new Headers({ 'content-length': '4092319' })
  assert.equal(forwardedContentLength(headers), '4092319')
})

test('an identity-encoded response keeps its Content-Length', () => {
  const headers = new Headers({ 'content-length': '12', 'content-encoding': 'identity' })
  assert.equal(forwardedContentLength(headers), '12')
})

test('a compressed response drops it — undici already decompressed the body', () => {
  // Forwarding the compressed length here would describe the body we are
  // sending as shorter than it is, and the client would truncate it.
  const headers = new Headers({ 'content-length': '512', 'content-encoding': 'gzip' })
  assert.equal(forwardedContentLength(headers), null)
})

test('the encoding check is case- and whitespace-insensitive', () => {
  const headers = new Headers({ 'content-length': '512', 'content-encoding': ' GZIP ' })
  assert.equal(forwardedContentLength(headers), null)
})

test('a response with no Content-Length stays without one', () => {
  // A streamed response legitimately has no length; inventing one would be worse
  // than omitting it.
  assert.equal(forwardedContentLength(new Headers()), null)
})
