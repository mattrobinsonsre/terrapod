/**
 * Next.js instrumentation hook — runs once on server startup.
 *
 * Starts a tiny HTTP server on port 9091 that serves Prometheus
 * metrics. This keeps metrics on a separate port from the main
 * Next.js server (3000), so the ingress never exposes them.
 * Prometheus scrapes this port via the ServiceMonitor.
 *
 * The actual server setup lives in instrumentation-node.ts to avoid
 * Turbopack's static analysis rejecting node:http in Edge Runtime
 * evaluation of this file.
 */

export async function register() {
  if (process.env.NEXT_RUNTIME !== 'nodejs') return

  const mod = await import('./instrumentation-node')
  mod.startMetricsServer()
}
