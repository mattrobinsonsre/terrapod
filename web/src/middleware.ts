import { NextRequest, NextResponse } from 'next/server'

const PROXY_PREFIXES = ['/api/', '/.well-known/', '/oauth/', '/v1/']

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
  matcher: ['/api/:path*', '/.well-known/:path*', '/oauth/:path*', '/v1/:path*'],
}
