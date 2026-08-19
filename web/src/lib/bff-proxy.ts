// The BFF's single proxy implementation (#884, #1381).
//
// Every path the BFF forwards to the API goes through `proxy()` — `/api/*`,
// and the small-bodied prefixes `/v1/*`, `/oauth/*`, `/.well-known/*`. There is
// deliberately ONE implementation, because for a long time there were two and
// only one of them handled failure.
//
// The other was `NextResponse.rewrite` in middleware. It works, right up until
// the connection to the API fails: the rewrite is fire-once, so there is no
// retry, and the error surfaces as Next's own bare 500 with no way to catch it
// or choose a status. A single transient reset on one hop of a `tofu init`
// therefore failed an entire run, and reported it as though Terrapod itself had
// a bug. Routing every prefix through this file is what makes the handling
// below apply everywhere rather than to `/api/*` alone.
//
// Streaming (#884): the request body MUST be forwarded unbuffered. Config
// version tarballs, state blobs and provider binaries are tens to hundreds of
// MB, and the API streams them straight to object storage (rule 14). Next.js
// middleware buffers the whole body and caps it at `middlewareClientMaxBodySize`
// (10 MB), which silently truncates larger uploads — the other reason not to
// proxy there. A Route Handler reads `request.body` as a stream and forwards it
// with fetch's `duplex: 'half'`.
//
// Runtime config (#47): `API_URL` is read per request, not baked at build time,
// so the Helm-injected value is respected. That is why this is not a
// `next.config` rewrite to the API either.
//
// SSE responses stream back the same way — the upstream `Response.body` is
// returned unbuffered, and the `Content-Encoding: none` headers in
// next.config.js keep Next from gzip-buffering them.

// Deliberately no `next/server` import: everything used below is on the
// standard Request (url, method, headers, body, signal), so the proxy stays
// framework-agnostic and its unit test can import it without a Next runtime.

// Hop-by-hop / framing headers we must not forward verbatim — undici sets the
// host and manages transfer framing itself; forwarding a stale content-length
// alongside a streamed body desyncs the upstream request.
const STRIP_REQUEST_HEADERS = new Set([
  'host',
  'connection',
  'keep-alive',
  'proxy-connection',
  'transfer-encoding',
  'upgrade',
  'content-length',
  'expect',
])

// Response framing headers undici/Next re-derive for the streamed body.
const STRIP_RESPONSE_HEADERS = new Set([
  'content-encoding',
  'content-length',
  'transfer-encoding',
  'connection',
  'keep-alive',
])

// Methods that may be retried after a failed connection attempt. A retry here
// is safe for any method — we only retry when the connection failed before a
// single response byte arrived, so the upstream cannot have acted on it — but
// that reasoning depends on the failure being a connect/reset, and a proxy
// cannot always tell a reset-before-read from a reset-mid-read on a request
// whose body it streamed. Restricting retries to methods that are idempotent by
// definition means we never have to be right about that distinction.
const RETRYABLE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

// Transport-level failures worth a second attempt. The one that matters in
// practice is the keep-alive idle-close race: the client picks a pooled socket
// at the instant the server closes it for idleness, and cannot distinguish that
// from the server dying. It presents as ECONNRESET / "socket hang up", and it
// is the reason reverse proxies retry idempotent requests at all.
const RETRYABLE_CODES = new Set([
  'ECONNRESET',
  'ECONNREFUSED',
  'EPIPE',
  'ETIMEDOUT',
  'UND_ERR_SOCKET',
  'UND_ERR_CONNECT_TIMEOUT',
])

function apiBase(): string {
  // Read at request time so the Helm-injected env var is respected (#47).
  return process.env.API_URL || 'http://localhost:8001'
}

/**
 * Whether a failed fetch is a transport failure worth one more attempt.
 *
 * Walks the cause chain: undici reports the OS-level code several levels below
 * the error you catch, so inspecting only the top-level error finds nothing.
 * Exported for its unit test — the decision is the load-bearing part.
 */
export function isRetryableTransportError(err: unknown): boolean {
  const seen = new Set<unknown>()
  let cur: unknown = err
  for (let depth = 0; cur instanceof Error && depth < 8; depth++) {
    if (seen.has(cur)) break // a self-referencing cause must not spin
    seen.add(cur)
    const code = (cur as NodeJS.ErrnoException).code
    if (code && RETRYABLE_CODES.has(code)) return true
    // A reset can arrive with no code at all — "socket hang up" is exactly what
    // was logged in #1381.
    if (cur.message === 'socket hang up') return true
    cur = (cur as { cause?: unknown }).cause
  }
  return false
}

/**
 * Whether a request may be replayed after a failed connection attempt.
 *
 * Two conditions, both required. The method must be idempotent by definition,
 * so we never have to prove the upstream did not act on the first attempt. And
 * it must not carry a body: the body is a stream the first attempt consumed, so
 * a second attempt would send a bodiless request rather than the same one.
 */
export function isRetryableRequest(method: string, hasBody = false): boolean {
  return !hasBody && RETRYABLE_METHODS.has(method.toUpperCase())
}

function isClientAbort(err: unknown): boolean {
  return err instanceof Error && err.name === 'AbortError'
}

/**
 * Forward a request to the API, streaming both directions.
 *
 * `stripPrefix` removes an internal routing prefix from the path before
 * forwarding — the `/v1`, `/oauth` and `/.well-known` prefixes are rewritten
 * onto an internal route so they can share this handler, and the API must still
 * see their original paths.
 */
export async function proxy(request: Request, stripPrefix?: string): Promise<Response> {
  const incoming = new URL(request.url)
  const target = new URL(apiBase())
  target.pathname =
    stripPrefix && incoming.pathname.startsWith(stripPrefix)
      ? incoming.pathname.slice(stripPrefix.length) || '/'
      : incoming.pathname
  target.search = incoming.search

  const headers = new Headers()
  request.headers.forEach((value, key) => {
    if (!STRIP_REQUEST_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value)
    }
  })

  const method = request.method.toUpperCase()
  const hasBody = method !== 'GET' && method !== 'HEAD'

  const init: RequestInit & { duplex?: 'half' } = {
    method,
    headers,
    redirect: 'manual',
    cache: 'no-store',
    signal: request.signal,
  }
  if (hasBody) {
    // Stream the request body straight through — never buffer it. `duplex:
    // 'half'` is required to send a streaming body with fetch (Node/undici).
    init.body = request.body
    init.duplex = 'half'
  }

  // A streamed request body is consumed by the first attempt and cannot be
  // replayed, so only bodyless idempotent requests are eligible for a retry.
  const mayRetry = isRetryableRequest(method, hasBody)

  let upstream: Response
  let attempt = 0
  for (;;) {
    try {
      upstream = await fetch(target, init)
      break
    } catch (err) {
      // A client that hung up is not a proxy failure.
      if (isClientAbort(err)) {
        return new Response(null, { status: 499 }) // client closed request
      }
      if (mayRetry && attempt === 0 && isRetryableTransportError(err)) {
        attempt++
        // No backoff: a stale pooled socket fails instantly and the retry opens
        // a fresh connection, so waiting only adds latency to the request that
        // is already the slow one. A genuinely down API fails the retry too and
        // we return 502 immediately, rather than holding the request open.
        continue
      }
      console.error(
        `BFF proxy failed: ${method} ${target.pathname}` +
          (attempt > 0 ? ` (after ${attempt} retry)` : ''),
        err
      )
      // 502, not 500: the BFF is fine — something it depends on was not
      // reachable. Same distinction #1358 drew for VCS provider outages.
      return new Response('Bad Gateway', { status: 502 })
    }
  }

  const responseHeaders = new Headers()
  upstream.headers.forEach((value, key) => {
    if (!STRIP_RESPONSE_HEADERS.has(key.toLowerCase())) {
      responseHeaders.set(key, value)
    }
  })

  // Stream the upstream body back unbuffered (preserves SSE + large downloads).
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  })
}
