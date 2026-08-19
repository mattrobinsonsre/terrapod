// BFF proxy for /v1/*, /oauth/* and /.well-known/* (#1381).
//
// These reach here via internal rewrites in next.config.js, which is what lets
// them share the one proxy implementation with /api/*. They used to be proxied
// by `NextResponse.rewrite` in middleware instead, which cannot catch an
// upstream connection failure or retry it — a transient reset became a bare 500
// and failed the run that hit it.
//
// The rewrite is internal (this route, in this process), not a proxy to the
// API, so nothing about it is subject to the runtime-config problem that keeps
// the real proxying out of next.config (#47).
//
// `.well-known` is the reason for the prefix indirection rather than three
// sibling route directories: Next's file-system router ignores dot-prefixed
// folders, so `app/.well-known/` would never match. The marker must not start
// with an underscore either — that marks a PRIVATE folder, which Next excludes
// from routing, so every rewrite would 404 while the build stayed green.

import { type NextRequest } from 'next/server'
import { proxy } from '@/lib/bff-proxy'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

// This route is reachable directly, not only via the rewrites, so it is pinned
// to exactly the prefixes those rewrites feed it. Without this, `/bff/<path>`
// would proxy any path to the API — a wider surface than the three prefixes we
// mean to expose, for no benefit.
const ALLOWED = ['/v1/', '/oauth/', '/.well-known/']

const handler = (request: NextRequest) => {
  const path = new URL(request.url).pathname.slice('/bff'.length)
  if (!ALLOWED.some((p) => path.startsWith(p))) {
    return Promise.resolve(new Response('Not Found', { status: 404 }))
  }
  // Strip the marker so the API sees the path the client asked for.
  return proxy(request, '/bff')
}

export const GET = handler
export const POST = handler
export const PUT = handler
export const PATCH = handler
export const DELETE = handler
export const HEAD = handler
export const OPTIONS = handler
