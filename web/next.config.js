/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  allowedDevOrigins: ['terrapod.local'],
  // Prevent gzip buffering on SSE endpoints. Next.js compression buffers
  // small messages (keepalives, events) indefinitely, breaking real-time
  // streaming. Setting Content-Encoding: none tells the compression
  // middleware to pass these responses through unmodified.
  //
  // SSE endpoints all moved from /api/v2 to /api/terrapod/v1 in #269. The
  // /api/v2 entries are kept until the deprecated alias is dropped in
  // v0.24.0 (#278).
  async headers() {
    return [
      // Canonical /api/terrapod/v1 SSE paths
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
      // Deprecated /api/v2 aliases — kept until v0.24.0 (#278)
      {
        source: '/api/v2/listeners/:path*',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/v2/workspaces/:path*/runs/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/v2/workspace-events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/v2/agent-pools/:path*/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
    ]
  },
}

module.exports = nextConfig
