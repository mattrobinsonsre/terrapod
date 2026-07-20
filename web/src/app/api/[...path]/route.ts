// Streaming BFF proxy for /api/* (issue #884).
//
// The BFF forwards every /api/* request to the API service. This MUST stream
// the request body — it must never buffer it. Config-version tarballs, state
// blobs, and provider binaries are tens to hundreds of MB, and the API is
// carefully written to stream them straight to object storage (rule 14). If the
// BFF buffers first, that streaming is defeated.
//
// Why a Route Handler and not middleware: Next.js middleware buffers the whole
// request body and caps it at `middlewareClientMaxBodySize` (10 MB default), so
// any upload over that silently truncates and the proxied request dies with a
// `socket hang up` (a 500 the client sees as "error connecting to the cloud
// backend", with no run ever created). A Route Handler has no such cap: it
// reads `request.body` as a ReadableStream and forwards it with fetch's
// `duplex: 'half'`, so bytes flow client → BFF → API → storage without ever
// being fully held in the web pod's memory.
//
// Why not `next.config` rewrites (which also stream): those bake the
// destination at build time, so the Helm-injected runtime `API_URL` is ignored
// (#47 — the reason the proxy moved to middleware in the first place). A Route
// Handler reads `process.env.API_URL` per request, so it gets BOTH runtime
// config AND streaming — the two properties #47 and #884 each needed.
//
// SSE responses (…/events) stream back the same way: we return the upstream
// `Response.body` stream unbuffered. The `Content-Encoding: none` headers in
// next.config.js still apply by path and keep Next from gzip-buffering them.

import { type NextRequest } from 'next/server'

// Force dynamic + Node runtime: this is a per-request proxy, never statically
// optimised, and streaming request bodies via fetch needs the Node runtime.
export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

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

function apiBase(): string {
  // Read at request time so the Helm-injected env var is respected (#47).
  return process.env.API_URL || 'http://localhost:8001'
}

async function proxy(request: NextRequest): Promise<Response> {
  const incoming = new URL(request.url)
  const target = new URL(apiBase())
  target.pathname = incoming.pathname
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

  let upstream: Response
  try {
    upstream = await fetch(target, init)
  } catch (err) {
    // Client aborts surface as an AbortError — that's not a proxy failure.
    if (err instanceof Error && err.name === 'AbortError') {
      return new Response(null, { status: 499 }) // client closed request
    }
    return new Response('Bad Gateway', { status: 502 })
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

export const GET = proxy
export const POST = proxy
export const PUT = proxy
export const PATCH = proxy
export const DELETE = proxy
export const HEAD = proxy
export const OPTIONS = proxy
