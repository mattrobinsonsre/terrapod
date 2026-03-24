/**
 * Prometheus metrics for the Terrapod web frontend.
 *
 * Uses prom-client (server-side only). Provides default Node.js
 * runtime metrics (GC, event loop, memory) and custom counters
 * for tracking web-specific events.
 *
 * Note: The Next.js middleware runs in the Edge Runtime and cannot
 * use prom-client. BFF proxy request timing is tracked on the API
 * side via its HTTP middleware. Web metrics focus on frontend
 * server health and page-level events.
 */

import client from 'prom-client'

// Use a custom registry to avoid polluting the default
export const registry = new client.Registry()

registry.setDefaultLabels({ app: 'terrapod-web' })

// Collect default Node.js metrics (GC, event loop, memory)
client.collectDefaultMetrics({ register: registry })

export const webPageRequests = new client.Counter({
  name: 'terrapod_web_page_requests_total',
  help: 'Total page/route handler requests served by Next.js server',
  labelNames: ['path'] as const,
  registers: [registry],
})

export const webMetricsScrapes = new client.Counter({
  name: 'terrapod_web_metrics_scrapes_total',
  help: 'Total /metrics endpoint scrapes',
  registers: [registry],
})
