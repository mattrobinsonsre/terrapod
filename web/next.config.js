/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  // Prevent gzip buffering on SSE endpoints. Next.js compression buffers
  // small messages (keepalives, events) indefinitely, breaking real-time
  // streaming. Setting Content-Encoding: none tells the compression
  // middleware to pass these responses through unmodified.
  async headers() {
    return [
      {
        source: '/api/v2/listeners/:path*',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/v2/workspaces/:path*/runs/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/v2/workspaces/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/v2/admin/health/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
    ]
  },
}

module.exports = nextConfig
