import { NextRequest, NextResponse } from 'next/server'

const PROXY_PREFIXES = ['/api/', '/.well-known/', '/oauth/', '/v1/']

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl

  if (!PROXY_PREFIXES.some((prefix) => pathname.startsWith(prefix))) {
    return NextResponse.next()
  }

  const apiUrl = process.env.API_URL || 'http://localhost:8001'
  const target = new URL(pathname + request.nextUrl.search, apiUrl)

  return NextResponse.rewrite(target, {
    request: {
      headers: request.headers,
    },
  })
}

export const config = {
  matcher: ['/api/:path*', '/.well-known/:path*', '/oauth/:path*', '/v1/:path*'],
}
