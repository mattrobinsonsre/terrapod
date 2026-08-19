// BFF proxy for /api/* — see src/lib/bff-proxy.ts for the whole rationale
// (streaming, runtime config, retry, why this is a Route Handler).

import { type NextRequest } from 'next/server'
import { proxy } from '@/lib/bff-proxy'

// Force dynamic + Node runtime: this is a per-request proxy, never statically
// optimised, and streaming request bodies via fetch needs the Node runtime.
export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const handler = (request: NextRequest) => proxy(request)

export const GET = handler
export const POST = handler
export const PUT = handler
export const PATCH = handler
export const DELETE = handler
export const HEAD = handler
export const OPTIONS = handler
