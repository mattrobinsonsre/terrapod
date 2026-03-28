/**
 * Node.js-only instrumentation — metrics HTTP server.
 *
 * Separated from instrumentation.ts so Turbopack does not attempt
 * to bundle node:http for the Edge Runtime.
 */

import http from 'node:http'
import { registry, webMetricsScrapes } from '@/lib/metrics'

export function startMetricsServer() {
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
