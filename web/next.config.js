/** @type {import('next').NextConfig} */
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
    return [
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
  },
}

module.exports = nextConfig
