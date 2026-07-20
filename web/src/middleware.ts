import { NextRequest, NextResponse } from 'next/server'

// `/api/*` is proxied by the streaming Route Handler at app/api/[...path]/
// route.ts — NOT here. Middleware buffers the request body and caps it at
// `middlewareClientMaxBodySize` (10 MB), which truncates large uploads
// (config-version tarballs, state blobs, provider binaries) and breaks them
// with a `socket hang up` (#884). The remaining prefixes only ever carry small
// request bodies (service discovery, the OAuth token exchange, the registry
// CLI protocol), so the middleware rewrite is fine for them. Both this
// middleware and the route handler run inside the BFF (web) tier — all traffic
// still flows client → BFF → API (rule 8); only the /api proxy mechanism
// differs so it can stream.
const PROXY_PREFIXES = ['/.well-known/', '/oauth/', '/v1/']

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl

  if (!PROXY_PREFIXES.some((prefix) => pathname.startsWith(prefix))) {
    return NextResponse.next()
  }

  const apiUrl = process.env.API_URL || 'http://localhost:8001'
  const target = new URL(pathname + request.nextUrl.search, apiUrl)

  // Snapshot the headers into a plain Headers object before handing
  // them to NextResponse.rewrite. The Edge runtime's request.headers
  // is a proxy whose lifetime is tied to the original request; passing
  // it directly works for sequential traffic but is a footgun under
  // concurrent load where the proxy may be torn down before the
  // rewrite completes. The copy here is cheap and removes the failure
  // mode entirely.
  const headers = new Headers()
  request.headers.forEach((value, key) => {
    headers.set(key, value)
  })

  return NextResponse.rewrite(target, {
    request: { headers },
  })
}

export const config = {
  // `/api/:path*` is intentionally absent — the streaming Route Handler owns it
  // (see PROXY_PREFIXES note above). Keep the matcher in sync with it.
  matcher: ['/.well-known/:path*', '/oauth/:path*', '/v1/:path*'],
}
