/** @type {import('next').NextConfig} */

// HSTS (Strict-Transport-Security). Tells browsers to remember "always
// use HTTPS for THIS hostname" for max-age, so subsequent visits to
// http://hostname auto-upgrade to https:// without trying port 80.
// Set via the HSTS env var at runtime (the chart populates it from
// `web.hsts.value`); default below is 2 years.
//
// NO `includeSubDomains`: Terrapod's HSTS assertion is scoped to the
// hostname it's served at. The hostname's parent zone (e.g. `ts.net`
// for Tailscale-hosted deployments, or a corporate-internal zone) is
// almost always shared with other services that have their own HTTP
// policies — asserting `includeSubDomains` would force HTTPS on every
// sibling host the browser later visits, which is the deploying
// operator's call, not ours. Operators who DO own the entire parent
// zone can set `web.hsts.value` to include the directive explicitly.
//
// NO `preload`: that's a one-way commitment to the hsts-preload list
// at https://hstspreload.org — also operator's call.
//
// Set HSTS="" to disable the header entirely (e.g. mixed http/https
// deployments).
const HSTS_DEFAULT = 'max-age=63072000'
const hstsValue = process.env.HSTS ?? HSTS_DEFAULT

const nextConfig = {
  output: 'standalone',
  allowedDevOrigins: ['terrapod.local'],
  // Prevent gzip buffering on SSE endpoints. Next.js compression buffers
  // small messages (keepalives, events) indefinitely, breaking real-time
  // streaming. Setting Content-Encoding: none tells the compression
  // middleware to pass these responses through unmodified.
  //
  // SSE endpoints are all Terrapod-native at /api/terrapod/v1. The
  // transitional /api/v2 aliases (#269) were removed in v0.24.0 (#278).
  async headers() {
    const headers = [
      {
        source: '/api/terrapod/v1/listeners/:path*',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/terrapod/v1/workspaces/:path*/runs/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/terrapod/v1/workspace-events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/terrapod/v1/agent-pools/:path*/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
    ]
    if (hstsValue) {
      headers.push({
        source: '/:path*',
        headers: [{ key: 'Strict-Transport-Security', value: hstsValue }],
      })
    }
    return headers
  },
}

module.exports = nextConfig
