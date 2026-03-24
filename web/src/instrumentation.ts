/**
 * Next.js instrumentation hook — runs once on server startup.
 *
 * Starts a tiny HTTP server on port 9091 that serves Prometheus
 * metrics. This keeps metrics on a separate port from the main
 * Next.js server (3000), so the ingress never exposes them.
 * Prometheus scrapes this port via the ServiceMonitor.
 */

export async function register() {
  // Only run on the Node.js server, not Edge Runtime
  if (process.env.NEXT_RUNTIME !== 'nodejs') return

  // Dynamic import — node:http is not available in Edge Runtime
  const http = await import('node:http')
  const { registry, webMetricsScrapes } = await import('@/lib/metrics')

  const METRICS_PORT = parseInt(process.env.METRICS_PORT || '9091', 10)

  const server = http.createServer(async (_req, res) => {
    webMetricsScrapes.inc()
    const metrics = await registry.metrics()
    res.writeHead(200, { 'Content-Type': registry.contentType })
    res.end(metrics)
  })

  server.listen(METRICS_PORT, () => {
    console.log(`Metrics server listening on port ${METRICS_PORT}`)
  })
}
